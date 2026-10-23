"""一个最小的本地 MCP server，供集成测试与手工验证使用。

存在意义：MCP 集成需要一个**真实**的 server 才能验证协议往返，
而 ``npx`` / ``uvx`` 拉起来的第三方 server 既慢又需要网络。
这个脚本只依赖已装的 ``mcp`` 包，启动是百毫秒级，且行为完全确定。

两种用法：

* stdio（默认）—— ``python demo_server.py``，由客户端拉起子进程；
* streamable-http —— ``python demo_server.py --http --port 8931``，
  暴露 ``http://127.0.0.1:8931/mcp``。
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("doudou-demo", instructions="Doudou 集成测试用的最小 MCP server。")


@mcp.tool()
def echo(text: str) -> str:
    """原样返回传入的文本，用于验证工具往返。

    Args:
        text: 任意文本。
    """
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """计算两个整数之和。

    Args:
        a: 第一个加数。
        b: 第二个加数。
    """
    return a + b


@mcp.tool()
def delete_everything() -> str:
    """一个「危险」工具，用于验证 exclude_tools 能把它挡在模型视野之外。

    它只返回字符串、不做任何实际删除 —— 测试永远不应该有破坏性。
    """
    return "（这只是一个用于测试过滤的占位工具）"


def main(argv: list[str]) -> int:
    if "--http" in argv:
        port = 8931
        for index, item in enumerate(argv):
            if item == "--port" and index + 1 < len(argv):
                port = int(argv[index + 1])
        # FastMCP 的 host/port 在构造时传入，因此这里重建一个实例。
        http_mcp = FastMCP("doudou-demo-http", host="127.0.0.1", port=port)
        for name, func in (
            ("echo", echo),
            ("add", add),
            ("delete_everything", delete_everything),
        ):
            http_mcp.tool(name=name)(func.__wrapped__ if hasattr(func, "__wrapped__") else func)
        http_mcp.run(transport="streamable-http")
        return 0

    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
