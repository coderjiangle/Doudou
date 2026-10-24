"""Agent assembly package.

**这个 ``__init__`` 必须保持轻量**：任何 ``import doudou.agent.x`` 都会先执行它，
而 ``agent`` 与 ``tools`` 是互相依赖的（``agent.factory`` 要工具，``tools.registry``
要记忆与技能）。在这里 import ``factory`` 会立刻成环 ——
表现是「导入顺序不同就报 ImportError」，极难排查。

因此这里只导出**没有任何跨包依赖**的 ``events``；其余请显式导入具体模块，
例如 ``from doudou.agent.factory import build_agent``。
"""

from __future__ import annotations

from doudou.agent.events import RunEvent, ToolOutcome

__all__ = ["RunEvent", "ToolOutcome"]
