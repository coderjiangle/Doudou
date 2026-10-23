"""MCP 连接构造与工具命名空间（纯离线，不涉及网络）。"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from doudou.mcp.config import McpServerSpec
from doudou.mcp.loader import (
    PREFIX_SEPARATOR,
    build_connection,
    prefixed_name,
    rename_tool,
    split_prefixed_name,
)


@tool
def search(query: str) -> str:
    """搜索。"""
    return query


# --------------------------------------------------------------------------- 连接构造


def test_build_stdio_connection() -> None:
    spec = McpServerSpec(
        name="local",
        transport="stdio",
        command="python",
        args=["server.py", "--flag"],
        env={"K": "V"},
    )

    # 只放适配器认识的键；多余字段会被静默忽略或报错
    assert build_connection(spec) == {
        "transport": "stdio",
        "command": "python",
        "args": ["server.py", "--flag"],
        "env": {"K": "V"},
    }


def test_build_stdio_omits_empty_env() -> None:
    spec = McpServerSpec(name="local", transport="stdio", command="python")

    assert "env" not in build_connection(spec)
    assert "cwd" not in build_connection(spec)


def test_build_stdio_includes_cwd_when_set() -> None:
    spec = McpServerSpec(name="local", transport="stdio", command="python", cwd="/tmp/work")

    assert build_connection(spec)["cwd"] == "/tmp/work"


def test_build_http_connection_uses_streamable_http() -> None:
    spec = McpServerSpec(
        name="remote",
        transport="http",
        url="https://api.test/mcp",
        headers={"Authorization": "Bearer x"},
        timeout=30,
    )

    connection = build_connection(spec)

    # YAML 写 http，适配器要 streamable_http
    assert connection["transport"] == "streamable_http"
    assert connection["url"] == "https://api.test/mcp"
    assert connection["headers"] == {"Authorization": "Bearer x"}
    assert connection["timeout"] == 30


def test_build_sse_connection() -> None:
    spec = McpServerSpec(name="legacy", transport="sse", url="https://api.test/sse")

    assert build_connection(spec)["transport"] == "sse"
    assert "headers" not in build_connection(spec)


# --------------------------------------------------------------------------- 命名空间


def test_prefixed_name() -> None:
    assert prefixed_name("github", "search") == "github__search"
    assert PREFIX_SEPARATOR == "__"


def test_rename_tool_keeps_schema_and_description() -> None:
    """直接改名字而不是包壳：包壳会丢掉 args_schema，模型看到的定义就残了。"""
    original_schema = search.args_schema
    original_description = search.description

    renamed = rename_tool(search, server="srv")

    assert renamed.name == "srv__search"
    assert renamed.args_schema is original_schema
    assert renamed.description == original_description


def test_split_prefixed_name() -> None:
    assert split_prefixed_name("github__search") == ("github", "search")
    assert split_prefixed_name("plain") == (None, "plain")


def test_two_servers_with_same_tool_name_do_not_collide() -> None:
    @tool
    def alpha() -> str:
        """a。"""
        return "a"

    @tool
    def beta() -> str:
        """b。"""
        return "b"

    first = rename_tool(alpha, server="srv1")
    second = rename_tool(beta, server="srv2")

    assert first.name != second.name
    assert isinstance(first, BaseTool)
