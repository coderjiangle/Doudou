"""CliChannel Markdown 渲染、块切分与交互边界单元测试。"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from rich.console import Console
from rich.markdown import Markdown

from doudou.agent.events import RunEvent
from doudou.agent.factory import build_agent
from doudou.agent.testing import ScriptedChatModel
from doudou.channels.cli_channel import CliChannel, _split_completed_blocks
from doudou.config.loader import Settings
from doudou.gateway.runner import AgentRunner
from doudou.tools.decorators import annotate


def _make_tty_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=80, color_system="standard")
    return console, buf


def _make_plain_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=80, color_system=None)
    return console, buf


# --------------------------------------------------------------------------- 块切分


def test_split_completed_blocks_commits_at_blank_line() -> None:
    committed, tail = _split_completed_blocks("# 标题\n\n第一段\n\n未完结")

    assert committed == "# 标题\n\n第一段\n\n"
    assert tail == "未完结"


def test_split_completed_blocks_never_splits_inside_fence() -> None:
    text = "```python\ndef f():\n\n    return 1\n```\n\n尾段"
    partial = "```python\ndef f():\n\n    return 1"

    assert _split_completed_blocks(partial) == ("", partial)
    committed, tail = _split_completed_blocks(text)
    assert committed == "```python\ndef f():\n\n    return 1\n```\n\n"
    assert tail == "尾段"


def test_split_completed_blocks_ignores_inline_backticks() -> None:
    text = "使用 ```foo``` 示例\n\n下一段"

    committed, tail = _split_completed_blocks(text)

    assert committed == "使用 ```foo``` 示例\n\n"
    assert tail == "下一段"


# --------------------------------------------------------------------------- CliChannel 渲染


async def test_cli_channel_renders_markdown_on_tty_stream(settings: Settings) -> None:
    model = ScriptedChatModel(
        steps=[
            AIMessage(content="# 标题\n\n这是 **加粗** 内容。\n\n```python\nprint(1)\n```\n\n尾段")
        ]
    )
    bundle = build_agent(settings, model=model, extra_tools=())
    runner = AgentRunner(bundle)
    console, buf = _make_tty_console()
    channel = CliChannel(runner, stream=True, console=console)

    result = await channel.ask("测试")

    assert result.ok
    rendered = buf.getvalue()
    # 经过 rich Markdown 渲染后，原始 Markdown 标记已被转成 ANSI 样式，不再保留原始 ** 与 ```
    assert "**加粗**" not in rendered
    assert "```" not in rendered
    assert "标题" in rendered
    assert "加粗" in rendered
    assert "print" in rendered
    assert "尾段" in rendered


async def test_cli_channel_renders_markdown_on_tty_non_stream(settings: Settings) -> None:
    model = ScriptedChatModel(steps=[AIMessage(content="## 小节\n\n- 条目 **A**")])
    bundle = build_agent(settings, model=model, extra_tools=())
    runner = AgentRunner(bundle)
    console, buf = _make_tty_console()
    channel = CliChannel(runner, stream=False, console=console)

    result = await channel.ask("测试")

    assert result.ok
    rendered = buf.getvalue()
    assert "**A**" not in rendered
    assert "## 小节" not in rendered
    assert "小节" in rendered
    assert "条目" in rendered


async def test_cli_channel_falls_back_to_raw_when_disabled_or_non_tty(
    settings: Settings,
) -> None:
    md = "# 标题\n\n这是 **加粗** 内容。"

    # 1. render_markdown=False（对应 --raw）即使在 TTY 下也保留原始 Markdown
    model_raw = ScriptedChatModel(steps=[AIMessage(content=md)])
    bundle_raw = build_agent(settings, model=model_raw, extra_tools=())
    tty_console, tty_buf = _make_tty_console()
    channel_raw = CliChannel(
        AgentRunner(bundle_raw),
        stream=True,
        console=tty_console,
        render_markdown=False,
    )
    await channel_raw.ask("测试")
    assert "**加粗**" in tty_buf.getvalue()
    assert tty_buf.getvalue().count("# 标题") == 1

    # 2. 非 TTY（重定向/管道/测试捕获）自动回退纯文本
    model_pipe = ScriptedChatModel(steps=[AIMessage(content=md)])
    bundle_pipe = build_agent(settings, model=model_pipe, extra_tools=())
    plain_console, plain_buf = _make_plain_console()
    channel_pipe = CliChannel(
        AgentRunner(bundle_pipe),
        stream=True,
        console=plain_console,
        render_markdown=True,
    )
    await channel_pipe.ask("测试")
    assert "**加粗**" in plain_buf.getvalue()
    assert plain_buf.getvalue().count("# 标题") == 1


async def test_cli_channel_flushes_live_before_tool_call_and_approval(
    settings: Settings,
) -> None:
    """工具调用（及触发 Confirm.ask）之前必须停掉 Live 并固化前置 Markdown 文本。"""
    live_states_at_approval: list[bool] = []

    @tool
    def echo_tool(msg: str) -> str:
        """回显文本。"""
        return f"done:{msg}"

    annotate(echo_tool, group="test", side_effects=True)

    console, buf = _make_tty_console()

    model = ScriptedChatModel(
        steps=[
            AIMessage(
                content="我先**查一下**。",
                tool_calls=[{"id": "call_1", "name": "echo_tool", "args": {"msg": "hi"}}],
            ),
            AIMessage(content="最终结论：**完成**。"),
        ]
    )

    channel_ref: list[CliChannel] = []

    async def approval_hook(_req: object) -> bool:
        # 此时 Live 必须已被停止，避免与交互确认打架
        assert channel_ref
        live_states_at_approval.append(channel_ref[0]._state.live is None)
        return True

    bundle = build_agent(
        settings,
        model=model,
        extra_tools=[echo_tool],
        approval_hook=approval_hook,
    )
    channel = CliChannel(AgentRunner(bundle), stream=True, console=console)
    channel_ref.append(channel)

    result = await channel.ask("执行")

    assert result.ok
    assert live_states_at_approval == [True]
    assert channel._state.live is None
    assert channel._state.status is None
    rendered = buf.getvalue()
    assert "查一下" in rendered
    assert "echo_tool" in rendered
    assert "完成" in rendered
    assert "**查一下**" not in rendered
    assert "**完成**" not in rendered


async def test_cli_channel_stops_live_on_stream_exception(settings: Settings) -> None:
    """事件流中途抛错时也要保证 Live 被停掉，不泄露隐藏光标状态。"""
    model = ScriptedChatModel(steps=[AIMessage(content="ok")])
    bundle = build_agent(settings, model=model, extra_tools=())
    console, _buf = _make_tty_console()
    channel = CliChannel(AgentRunner(bundle), stream=True, console=console)

    async def broken_stream() -> AsyncIterator[RunEvent]:
        yield RunEvent.text_delta("正在生成 **半段**", step=1)
        raise RuntimeError("boom")

    try:
        await channel._consume(broken_stream(), thread_id="t-err")
    except RuntimeError:
        pass

    assert channel._state.live is None
    assert channel._state.status is None


@pytest.mark.parametrize("send_deltas", [True, False])
async def test_cli_channel_stops_live_on_run_end_before_runner_finally(
    settings: Settings,
    send_deltas: bool,
) -> None:
    """收到 run_end 时就必须停掉 Live，避免 runner.finally 打日志时穿插并造成重复输出。"""
    model = ScriptedChatModel(steps=[AIMessage(content="行吧，**起来抖抖毛**。")])
    bundle = build_agent(settings, model=model, extra_tools=())
    console, buf = _make_tty_console()
    channel = CliChannel(AgentRunner(bundle), stream=True, console=console)

    live_at_post_run_end: list[bool] = []

    async def stream_with_post_log() -> AsyncIterator[RunEvent]:
        if send_deltas:
            yield RunEvent.text_delta("行吧，", step=1)
            yield RunEvent.text_delta("**起来抖抖毛**。", step=1)
        yield RunEvent.run_end("行吧，**起来抖抖毛**。", step=1)
        # 模拟 AgentRunner.astream finally 阶段（run_end 之后、迭代器退出之前）
        live_at_post_run_end.append(channel._state.live is None)

    with patch.object(console, "print", wraps=console.print) as printed:
        result = await channel._consume(stream_with_post_log(), thread_id="t-end")
        channel._finish(result)

    assert live_at_post_run_end == [True]
    assert "起来抖抖毛" in buf.getvalue()
    # Rich 的临时预览随平台变化，这里只统计实际固化的 Markdown。
    assert (
        sum(
            bool(call.args) and isinstance(call.args[0], Markdown)
            for call in printed.call_args_list
        )
        == 1
    )


async def test_cli_channel_threads_command_lists_and_switches_thread(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/threads`` 应列出历史会话，选择编号后可继续在该会话对话。"""
    model = ScriptedChatModel(
        steps=[
            AIMessage(content="收到一"),
            AIMessage(content="收到二"),
            AIMessage(content="收到三"),
        ]
    )
    bundle = build_agent(settings, model=model, extra_tools=())
    runner = AgentRunner(bundle)
    console, buf = _make_plain_console()
    channel = CliChannel(runner, stream=False, console=console)

    await channel.ask("第一条消息")
    tid1 = channel._active_threads["local"]
    await channel._handle_command("/new", conversation_id="local")
    await channel.ask("第二条消息")
    tid2 = channel._active_threads["local"]
    assert tid1 != tid2

    listed = await runner.list_threads()
    tid1_index = next(i for i, item in enumerate(listed, start=1) if item.thread_id == tid1)
    monkeypatch.setattr(console, "input", lambda _prompt="": str(tid1_index))

    keep_going = await channel._handle_command("/threads", conversation_id="local")
    assert keep_going is True

    output = buf.getvalue()
    assert "历史会话" in output
    assert tid1 in output
    assert "已切换到会话" in output
    assert "第一条消息" in output

    await channel.ask("回到第一条会话")
    hist1 = [str(m.content) for m in await runner.history(tid1)]
    hist2 = [str(m.content) for m in await runner.history(tid2)]
    assert "回到第一条会话" in hist1
    assert "回到第一条会话" not in hist2


async def test_cli_channel_stops_thinking_status_before_approval_and_on_end(
    settings: Settings,
) -> None:
    """等待模型期间开启 thinking status，进入工具确认前与结束时必须清理干净。"""

    @tool
    def echo_tool(msg: str) -> str:
        """回显文本。"""
        return f"ok:{msg}"

    annotate(echo_tool, group="test", side_effects=True)
    console, _buf = _make_tty_console()

    model = ScriptedChatModel(
        steps=[
            AIMessage(
                content="",
                tool_calls=[{"id": "call_1", "name": "echo_tool", "args": {"msg": "hi"}}],
            ),
            AIMessage(content="处理完成。"),
        ]
    )

    channel_ref: list[CliChannel] = []
    status_at_approval: list[bool] = []
    status_seen_during_stream: list[bool] = []

    async def approval_hook(_req: object) -> bool:
        status_at_approval.append(channel_ref[0]._state.status is None)
        return True

    bundle = build_agent(
        settings, model=model, extra_tools=[echo_tool], approval_hook=approval_hook
    )
    runner = AgentRunner(bundle)
    channel = CliChannel(runner, stream=True, console=console)
    channel_ref.append(channel)

    original_render = channel._render

    def spy_render(event: RunEvent) -> None:
        if event.type == "run_start":
            status_seen_during_stream.append(channel._state.status is not None)
        original_render(event)

    channel._render = spy_render  # type: ignore[method-assign]
    result = await channel.ask("测试等待状态")

    assert result.ok
    assert status_seen_during_stream == [True]
    assert status_at_approval == [True]
    assert channel._state.status is None
    assert channel._state.live is None
