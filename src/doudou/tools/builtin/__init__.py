"""Builtin tool providers.

每个子模块导出一个 ``build_tools(settings) -> Sequence[BaseTool]``，
由 :mod:`doudou.tools.registry` 统一装配。
"""

from __future__ import annotations

from doudou.tools.builtin import clock

__all__ = ["clock"]
