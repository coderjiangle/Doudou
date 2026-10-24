"""Tooling package: registry, annotations and builtin tool providers.

**这个 ``__init__`` 必须保持轻量**：任何 ``import doudou.tools.x`` 都会先执行它，
而 ``tools.registry`` 要 import ``agent.memory`` / ``skills.registry``，
``agent.react`` 又要 import ``tools.decorators`` —— 在这里 import ``registry``
会成环（与 ``agent`` / ``channels`` 的 ``__init__`` 同一个坑）。

因此这里只导出无跨包依赖的 ``decorators``；其余请显式导入具体模块，
例如 ``from doudou.tools.registry import build_tools``。
"""

from __future__ import annotations

from doudou.tools.decorators import ToolAnnotations, annotate, annotations_of

__all__ = ["ToolAnnotations", "annotate", "annotations_of"]
