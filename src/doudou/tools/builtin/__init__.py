"""Builtin tool providers.

每个子模块导出一个 ``build_tools(settings) -> Sequence[BaseTool]``，
由 :mod:`doudou.tools.registry` 统一装配。

「按配置装配」的一条重要约定：**缺依赖的工具不注册**，而不是注册一个永远失败的。
例：没有 ``TAVILY_API_KEY`` 就没有 ``web_search``；``tools.http.allowed_domains``
为空就没有 ``http_request``。这样工具列表恒等于「现在真的能用」的能力。
"""

from __future__ import annotations

from doudou.tools.builtin import clock, files, http, notes, skills, web_search

__all__ = ["clock", "files", "http", "notes", "skills", "web_search"]
