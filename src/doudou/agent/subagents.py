"""Subagent definitions from ``config.yaml``'s ``agent.subagents`` section.

子 Agent 的价值是**上下文隔离**：主 Agent 把一团乱麻（检索、比对、试错）丢给子 Agent，
子 Agent 在自己的上下文里折腾完，只回传一段结论。主 Agent 的窗口因此保持干净。

``SubAgentSpec`` 是配置结构的一部分，定义在 :mod:`doudou.config.schema`；
这里负责注册表封装与工具白名单筛选，执行在 ``tools/builtin/delegation.py``。

两个约束：

* **子 Agent 不继承 ``task`` 工具** —— 否则一个子 Agent 又能派子 Agent，
  递归深度不可控；
* **工具白名单优先**：省略 ``tools`` 时继承，显式空列表时禁用，非空时只允许列出的工具。
  子 Agent 的职责越窄，它跑偏的空间就越小。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from doudou.config.schema import AgentConfig, SubAgentSpec
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 子 Agent 不允许拿到的工具：给了它就能无限嵌套。
FORBIDDEN_TOOLS = frozenset({"task"})


class SubAgentError(ValueError):
    """子 Agent 定义不合法。"""


class SubAgentRegistry:
    """已加载的子 Agent 定义。"""

    def __init__(self, specs: Sequence[SubAgentSpec] = ()) -> None:
        self._specs = list(specs)
        self._by_name = {spec.name: spec for spec in self._specs}

    def __len__(self) -> int:
        return len(self._specs)

    @property
    def specs(self) -> list[SubAgentSpec]:
        return list(self._specs)

    @property
    def names(self) -> list[str]:
        return [spec.name for spec in self._specs]

    def get(self, name: str) -> SubAgentSpec | None:
        return self._by_name.get(name.strip())

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "tools": list(spec.tools) if spec.tools is not None else None,
                "skills": list(spec.skills),
                "model": spec.model,
                "max_steps": spec.max_steps,
            }
            for spec in self._specs
        ]


def subagents_from_config(config: AgentConfig) -> SubAgentRegistry:
    """把 ``config.yaml`` 的 ``agent.subagents`` 组装成注册表。"""
    specs = list(config.subagents)
    if specs:
        logger.info("subagents.loaded", names=[spec.name for spec in specs])
    return SubAgentRegistry(specs)


def select_tools(spec: SubAgentSpec, available: Sequence[Any]) -> list[Any]:
    """按白名单挑出子 Agent 能用的工具。

    永远排除 ``task``（防无限嵌套）；省略白名单时继承，显式空列表时禁用工具。
    白名单里写了不存在的工具会告警 —— 静默忽略会让「我明明配了工具」变成谜。
    """
    candidate = [tool for tool in available if tool.name not in FORBIDDEN_TOOLS]
    if spec.tools is None:
        return candidate

    wanted = set(spec.tools)
    found = [tool for tool in candidate if tool.name in wanted]
    missing = sorted(wanted - {tool.name for tool in found})
    if missing:
        logger.warning(
            "subagents.unknown_tools",
            subagent=spec.name,
            missing=missing,
            available=sorted(tool.name for tool in candidate)[:20],
        )
    return found


@dataclass(slots=True)
class DelegationResult:
    """一次委派的结果。"""

    subagent: str
    text: str
    ok: bool = True
    error: str | None = None
    steps: int = 0
    tools: list[str] = field(default_factory=list)


__all__ = [
    "FORBIDDEN_TOOLS",
    "DelegationResult",
    "SubAgentError",
    "SubAgentRegistry",
    "SubAgentSpec",
    "select_tools",
    "subagents_from_config",
]
