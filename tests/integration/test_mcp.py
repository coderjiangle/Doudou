"""MCP 集成：接通**真实**的本地 server（stdio 与 streamable-http）。

这里不 mock 协议层：真的拉起子进程 / 真的开一个本地端口，验证的是
「清单 → 连接 → 工具 → LangChain 工具」整条链路。用的是
``tests/fixtures/mcp/demo_server.py``，只依赖已装的 ``mcp`` 包，启动百毫秒级。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
import yaml

from doudou.config.loader import Settings
from doudou.mcp.config import load_manifest
from doudou.mcp.lifecycle import McpRuntime
from doudou.tools.decorators import annotations_of
from doudou.utils.text import stringify_tool_result

SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "demo_server.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stdio_entry(name: str = "demo", **overrides: object) -> dict:
    entry: dict = {
        "name": name,
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(SERVER_SCRIPT)],
    }
    entry.update(overrides)
    return entry


def write_manifest(tmp_path: Path, servers: list[dict]) -> Path:
    path = tmp_path / "mcp.servers.yaml"
    path.write_text(
        yaml.safe_dump({"version": 1, "servers": servers}, allow_unicode=True),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def settings_factory_local(settings_factory):  # type: ignore[no-untyped-def]
    """构造一个把 ``mcp.config_file`` 指向临时清单的 Settings。"""

    def make(path: Path, *, enabled: bool = True, fail_fast: bool = False) -> Settings:
        config_yaml = (
            "mcp:\n"
            f"  enabled: {'true' if enabled else 'false'}\n"
            f"  fail_fast: {'true' if fail_fast else 'false'}\n"
            f'  config_file: "{path.as_posix()}"\n'
        )
        return settings_factory(config_yaml=config_yaml)

    return make


# --------------------------------------------------------------------------- stdio


async def test_stdio_server_tools_are_fetched(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    path = write_manifest(tmp_path, [stdio_entry()])
    runtime = McpRuntime(settings_factory_local(path))

    tools = await runtime.start()

    names = sorted(tool.name for tool in tools)
    assert names == ["demo__add", "demo__delete_everything", "demo__echo"]
    status = runtime.statuses[0]
    assert status.ok is True
    assert status.error is None
    await runtime.stop()


async def test_tool_actually_executes_over_mcp(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    """真正的往返：调用会拉起 server 子进程、执行、返回结果。"""
    path = write_manifest(tmp_path, [stdio_entry()])
    runtime = McpRuntime(settings_factory_local(path))
    tools = {tool.name: tool for tool in await runtime.start()}

    # MCP 工具原样返回的是**内容块**；真正决定「模型看到什么」的是
    # stringify_tool_result（ReAct 循环用它把结果转成文本）。
    assert (
        stringify_tool_result(await tools["demo__echo"].ainvoke({"text": "你好"})) == "echo: 你好"
    )
    assert stringify_tool_result(await tools["demo__add"].ainvoke({"a": 20, "b": 22})) == "42"

    await runtime.stop()


async def test_agent_sees_flattened_mcp_result(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    """端到端：模型上下文里必须是纯文本，而不是 ``[{"type": "text", ...}]``。

    这层包装白花 token，其中的内部 id 还会诱导模型把它当成有效引用。
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from doudou.agent.react import ReactAgent
    from doudou.agent.testing import ScriptedChatModel, tool_call

    path = write_manifest(tmp_path, [stdio_entry()])
    runtime = McpRuntime(settings_factory_local(path))
    tools = await runtime.start()

    agent = ReactAgent(
        model=ScriptedChatModel(
            steps=[
                tool_call("demo__echo", {"text": "你好"}, call_id="c1"),
                AIMessage(content="好了"),
            ]
        ),
        tools=tools,
        system_prompt="",
    )

    result = await agent.ainvoke([HumanMessage(content="echo 一下")])

    assert result.text == "好了"
    tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert tool_messages, "工具应该被执行了"
    assert tool_messages[0].content == "echo: 你好"
    await runtime.stop()


async def test_include_tools_whitelist(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    path = write_manifest(tmp_path, [stdio_entry(include_tools=["echo", "add"])])
    runtime = McpRuntime(settings_factory_local(path))

    names = sorted(tool.name for tool in await runtime.start())

    assert names == ["demo__add", "demo__echo"]
    assert "delete_everything" in runtime.statuses[0].filtered_out
    await runtime.stop()


async def test_exclude_tools_blocks_dangerous_one(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    """排除危险工具是 MCP 的核心安全手段：它不该出现在模型的工具列表里。"""
    path = write_manifest(tmp_path, [stdio_entry(exclude_tools=["delete_everything"])])
    runtime = McpRuntime(settings_factory_local(path))

    names = [tool.name for tool in await runtime.start()]

    assert "demo__delete_everything" not in names
    assert "delete_everything" in runtime.statuses[0].filtered_out
    await runtime.stop()


async def test_read_only_flag_controls_approval(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    """第三方 server 默认按「有副作用」处理；声明只读后才不必逐次确认。"""
    unsafe_dir = tmp_path / "unsafe"
    unsafe_dir.mkdir()
    safe_dir = tmp_path / "safe"
    safe_dir.mkdir()
    unsafe_path = write_manifest(unsafe_dir, [stdio_entry()])
    safe_path = write_manifest(safe_dir, [stdio_entry(read_only=True)])

    unsafe = McpRuntime(settings_factory_local(unsafe_path))
    safe = McpRuntime(settings_factory_local(safe_path))

    unsafe_tools = {tool.name: tool for tool in await unsafe.start()}
    safe_tools = {tool.name: tool for tool in await safe.start()}

    assert annotations_of(unsafe_tools["demo__echo"]).side_effects is True
    assert annotations_of(safe_tools["demo__echo"]).side_effects is False
    assert annotations_of(unsafe_tools["demo__echo"]).group == "mcp:demo"

    await unsafe.stop()
    await safe.stop()


async def test_failed_server_does_not_break_others(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    """关掉其中一个 server，服务仍然可用。"""
    path = write_manifest(
        tmp_path,
        [
            stdio_entry("good"),
            {
                "name": "broken",
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-c", "import sys; sys.exit(3)"],
            },
        ],
    )
    runtime = McpRuntime(settings_factory_local(path))

    tools = await runtime.start()

    statuses = {status.name: status for status in runtime.statuses}
    assert statuses["good"].ok is True
    assert statuses["broken"].ok is False
    assert statuses["broken"].error
    # 好的那个照常提供工具
    assert any(tool.name.startswith("good__") for tool in tools)
    assert not any(tool.name.startswith("broken__") for tool in tools)
    await runtime.stop()


async def test_fail_fast_raises_on_broken_server(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    """``fail_fast: true`` 适合「MCP 是核心依赖」的部署。"""
    from doudou.mcp.config import McpConfigError

    path = write_manifest(
        tmp_path,
        [
            {
                "name": "broken",
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-c", "import sys; sys.exit(3)"],
            }
        ],
    )
    runtime = McpRuntime(settings_factory_local(path, fail_fast=True))

    with pytest.raises(McpConfigError, match="broken"):
        await runtime.start()


async def test_disabled_mcp_does_nothing(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    path = write_manifest(tmp_path, [stdio_entry()])
    runtime = McpRuntime(settings_factory_local(path, enabled=False))

    assert await runtime.start() == []
    assert runtime.started is False


async def test_invalid_manifest_degrades_to_no_mcp(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    """清单拼错不该让服务起不来，但必须**显式告警**而不是静默失效。"""
    path = tmp_path / "mcp.servers.yaml"
    path.write_text(
        "servers:\n  - name: s\n    transport: stdio\n    comand: typo\n", encoding="utf-8"
    )
    runtime = McpRuntime(settings_factory_local(path))

    assert await runtime.start() == []
    assert runtime.manager.specs == []


async def test_describe_reports_state(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    path = write_manifest(tmp_path, [stdio_entry(description="演示 server")])
    runtime = McpRuntime(settings_factory_local(path))
    await runtime.start()

    entry = runtime.describe()[0]

    assert entry.name == "demo"
    assert entry.transport == "stdio"
    assert entry.ok is True
    assert entry.description == "演示 server"
    assert "demo__echo" in entry.tools
    assert entry.target.endswith("demo_server.py")
    assert entry.elapsed is not None
    await runtime.stop()


async def test_refresh_picks_up_manifest_changes(tmp_path: Path, settings_factory_local) -> None:
    path = write_manifest(tmp_path, [stdio_entry(include_tools=["echo"])])
    runtime = McpRuntime(settings_factory_local(path))
    await runtime.start()
    assert [tool.name for tool in runtime.tools] == ["demo__echo"]

    # 放开白名单后刷新
    write_manifest(tmp_path, [stdio_entry()])
    runtime.manager.manifest = load_manifest(path, env={})
    tools = await runtime.refresh(reason="test")

    assert len(tools) == 3
    await runtime.stop()


# --------------------------------------------------------------------------- streamable http


async def test_streamable_http_server(tmp_path: Path, settings_factory_local) -> None:  # type: ignore[no-untyped-def]
    """远程 http server：真开一个本地端口跑 MCP。"""
    import asyncio

    port = free_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SERVER_SCRIPT),
        "--http",
        "--port",
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        # 等端口就绪
        for _ in range(100):
            if await _port_open("127.0.0.1", port):
                break
            await asyncio.sleep(0.1)
        else:
            pytest.skip("HTTP MCP server 未能启动")

        path = write_manifest(
            tmp_path,
            [
                {
                    "name": "remote",
                    "transport": "http",
                    "url": f"http://127.0.0.1:{port}/mcp",
                    "exclude_tools": ["delete_everything"],
                }
            ],
        )
        runtime = McpRuntime(settings_factory_local(path))

        tools = {tool.name: tool for tool in await runtime.start()}

        assert sorted(tools) == ["remote__add", "remote__echo"]
        assert stringify_tool_result(await tools["remote__add"].ainvoke({"a": 2, "b": 3})) == "5"
        assert (
            stringify_tool_result(await tools["remote__echo"].ainvoke({"text": "hi"})) == "echo: hi"
        )
        assert runtime.statuses[0].transport == "http"
        await runtime.stop()
    finally:
        process.terminate()
        await process.wait()


async def _port_open(host: str, port: int) -> bool:
    import asyncio

    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.5)
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True
