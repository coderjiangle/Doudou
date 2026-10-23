"""Helpers to declare tools with metadata.

工具元数据挂在 ``BaseTool.metadata`` 上（langchain 的自有字段，不必打补丁），
统一放在 ``metadata["doudou"]`` 下，避免与其它框架写入的键冲突。

存在的意义是让**安全默认值可以声明式地表达**：
``side_effects=True`` 的工具在需要人工确认的策略下会自动被拦下来，
不必再到 ``agent.interrupt_on`` 里逐个罗列 —— 加工具的人不可能忘记登记。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool

#: 元数据在 ``BaseTool.metadata`` 里的命名空间。
METADATA_NAMESPACE = "doudou"


@dataclass(frozen=True, slots=True)
class ToolAnnotations:
    """一个工具的声明式属性。"""

    #: 所属分组，用于 ``tools.enabled_groups`` 开关与清单展示。
    group: str | None = None
    #: 是否会产生副作用（写文件、发消息、调用外部写接口）。
    #: 只读工具必须保持 ``False``，否则每次调用都会被拦下来确认。
    side_effects: bool = False
    #: 单次调用超时（秒）；``None`` 表示用 Agent 的全局默认。
    timeout: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "side_effects": self.side_effects,
            "timeout": self.timeout,
        }


def annotate(
    tool: BaseTool,
    *,
    group: str | None = None,
    side_effects: bool = False,
    timeout: float | None = None,
) -> BaseTool:
    """给工具打上元数据并原样返回，便于写成 ``annotate(tool, ...)``。

    已有的 ``metadata``（可能是别的集成写的）会被保留。
    """
    payload = ToolAnnotations(group=group, side_effects=side_effects, timeout=timeout)
    metadata = dict(tool.metadata or {})
    metadata[METADATA_NAMESPACE] = payload.as_dict()
    tool.metadata = metadata
    return tool


def annotations_of(tool: BaseTool) -> ToolAnnotations:
    """读回元数据。字段缺失或类型不对时按「最保守」的默认值处理。

    故意不抛异常：元数据是可选信息，坏掉不应该让工具不可用。
    但 ``side_effects`` 无法判断时**不当成只读** —— 安全默认要偏向拦截。
    代价是「忘了打注解的只读工具」会被要求确认，因此
    :func:`doudou.tools.registry.build_tools` 会对未注解的工具发出告警，
    让这种遗漏立刻可见而不是长期潜伏。
    """
    metadata = tool.metadata or {}
    if not isinstance(metadata, dict):
        return ToolAnnotations(side_effects=True)
    raw = metadata.get(METADATA_NAMESPACE)
    if not isinstance(raw, dict):
        return ToolAnnotations(side_effects=True)

    group = raw.get("group")
    timeout = raw.get("timeout")
    return ToolAnnotations(
        group=group if isinstance(group, str) else None,
        side_effects=bool(raw.get("side_effects", True)),
        timeout=float(timeout) if isinstance(timeout, (int, float)) else None,
    )


def is_annotated(tool: BaseTool) -> bool:
    """该工具是否带上了本项目的元数据。"""
    metadata = tool.metadata or {}
    return isinstance(metadata, dict) and isinstance(metadata.get(METADATA_NAMESPACE), dict)


__all__ = [
    "METADATA_NAMESPACE",
    "ToolAnnotations",
    "annotate",
    "annotations_of",
    "is_annotated",
]
