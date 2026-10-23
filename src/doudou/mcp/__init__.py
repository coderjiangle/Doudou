"""Model Context Protocol integration package.

四个模块的分工：

* :mod:`doudou.mcp.config`    —— 读清单、``${ENV}`` 替换、严格校验（不碰网络）
* :mod:`doudou.mcp.loader`    —— 声明 → 适配器的连接字典、工具命名空间（不碰网络）
* :mod:`doudou.mcp.client`    —— 包一层 ``MultiServerMCPClient``，逐个 server 拉工具
* :mod:`doudou.mcp.lifecycle` —— 启停、刷新、状态，对上层暴露唯一门面
"""

from __future__ import annotations

from doudou.mcp.client import FetchResult, McpClientManager, ServerStatus
from doudou.mcp.config import McpConfigError, McpManifest, McpServerSpec, load_manifest
from doudou.mcp.lifecycle import McpRuntime

__all__ = [
    "FetchResult",
    "McpClientManager",
    "McpConfigError",
    "McpManifest",
    "McpRuntime",
    "McpServerSpec",
    "ServerStatus",
    "load_manifest",
]
