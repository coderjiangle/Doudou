"""Load MCP server tool lists at startup and on demand.

把「声明」翻译成「适配器认识的连接字典」，以及工具命名空间的处理。
单独成文件是因为这两件事都**不涉及网络**，因此可以完全离线测试。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from doudou.mcp.config import McpServerSpec
from doudou.observability.logging import get_logger

logger = get_logger(__name__)
#: 工具名前缀分隔符。用双下划线，和 LangChain 生态的常见约定一致。
PREFIX_SEPARATOR = "__"


def build_connection(spec: McpServerSpec) -> dict[str, Any]:
    """把声明转成 ``MultiServerMCPClient`` 需要的连接字典。

    只放适配器**认识**的键：多余字段会被它静默忽略或报错，
    因此这里显式挑选，而不是把 ``model_dump()`` 整个丢过去。
    """
    if spec.transport == "stdio":
        connection: dict[str, Any] = {
            "transport": "stdio",
            "command": spec.command,
            "args": list(spec.args),
        }
        if spec.env:
            connection["env"] = dict(spec.env)
        if spec.cwd:
            connection["cwd"] = spec.cwd
        return connection

    connection = {
        "transport": spec.mcp_transport_name,
        "url": spec.url,
        "timeout": spec.timeout,
    }
    if spec.headers:
        connection["headers"] = dict(spec.headers)
    return connection


def prefixed_name(server: str, tool: str) -> str:
    """``server__tool``。"""
    return f"{server}{PREFIX_SEPARATOR}{tool}"


def rename_tool(tool: BaseTool, *, server: str) -> BaseTool:
    """给工具名加 server 前缀，避免多 server 撞名。

    直接改 ``tool.name``（LangChain 的 ``BaseTool`` 允许赋值）而不是包一层壳 ——
    包壳会丢掉原有的 args_schema 与描述，模型看到的工具定义就残了。
    """
    tool.name = prefixed_name(server, tool.name)
    return tool


def split_prefixed_name(name: str) -> tuple[str | None, str]:
    """拆出 ``(server, tool)``；没有前缀时 server 为 ``None``。"""
    if PREFIX_SEPARATOR not in name:
        return None, name
    server, _, tool = name.partition(PREFIX_SEPARATOR)
    return server, tool


__all__ = [
    "PREFIX_SEPARATOR",
    "build_connection",
    "prefixed_name",
    "rename_tool",
    "split_prefixed_name",
]
