"""Collect builtin tools, MCP tools and plugin tools into one list."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.tools.builtin import clock

logger = get_logger(__name__)

#: 一个工具提供方：按配置构造一组工具。
ToolProvider = Callable[[Settings], Sequence[BaseTool]]

#: 内置工具提供方：``(分组名, 提供方)``。MCP 工具在 M5 通过 ``extra`` 注入。
BUILTIN_PROVIDERS: tuple[tuple[str, ToolProvider], ...] = (("clock", clock.build_tools),)

#: ``extra`` 工具的默认分组与来源。
EXTRA_GROUP = "mcp"
EXTRA_SOURCE = "mcp"


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """工具的展示信息，用于 ``doudou tools list`` 与 ``GET /tools``。"""

    name: str
    description: str
    group: str
    source: str
    args: dict[str, str]
    requires_approval: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "group": self.group,
            "source": self.source,
            "args": self.args,
            "requires_approval": self.requires_approval,
        }


def build_tools(
    settings: Settings,
    *,
    extra: Iterable[BaseTool] = (),
    extra_source: str = EXTRA_SOURCE,
) -> list[BaseTool]:
    """汇总所有工具，并做重名检测。

    Args:
        settings: 用于构造依赖配置的工具（如默认时区）。
        extra: 额外工具，例如 M5 中 MCP 拉取到的工具。
        extra_source: ``extra`` 的来源标记，仅用于日志。

    Returns:
        工具列表，顺序为「内置 → extra」。重名时**保留先出现的那个**并告警。
    """
    collected: list[BaseTool] = []
    seen: dict[str, str] = {}

    for group, provider in BUILTIN_PROVIDERS:
        for item in provider(settings):
            _add(collected, seen, item, source=f"builtin:{group}")

    for item in extra:
        _add(collected, seen, item, source=extra_source)

    return collected


def _add(
    collected: list[BaseTool],
    seen: dict[str, str],
    item: BaseTool,
    *,
    source: str,
) -> None:
    if item.name in seen:
        logger.warning(
            "tools.duplicate_name",
            tool=item.name,
            kept=seen[item.name],
            dropped=source,
        )
        return
    seen[item.name] = source
    collected.append(item)


def tool_infos(
    settings: Settings,
    *,
    extra: Iterable[BaseTool] = (),
    extra_group: str = EXTRA_GROUP,
    extra_source: str = EXTRA_SOURCE,
) -> list[ToolInfo]:
    """生成工具清单（含是否需要人工确认）。

    直接遍历 provider 而不是复用 :func:`build_tools` 的结果，
    这样每个工具的分组与来源在产生处就确定，无需事后反查。
    ``extra`` 用于 M5 的 MCP 工具。
    """
    interrupt_on = settings.app.agent.interrupt_on
    infos: list[ToolInfo] = []
    seen: set[str] = set()

    for group, provider in BUILTIN_PROVIDERS:
        for item in provider(settings):
            if item.name in seen:
                continue
            seen.add(item.name)
            infos.append(
                _make_info(
                    item,
                    group=group,
                    source=f"builtin:{group}",
                    interrupt_on=interrupt_on,
                )
            )

    for item in extra:
        if item.name in seen:
            continue
        seen.add(item.name)
        infos.append(
            _make_info(item, group=extra_group, source=extra_source, interrupt_on=interrupt_on)
        )

    return infos


def _make_info(
    item: BaseTool, *, group: str, source: str, interrupt_on: dict[str, bool]
) -> ToolInfo:
    return ToolInfo(
        name=item.name,
        description=(item.description or "").strip(),
        group=group,
        source=source,
        args=describe_args(item),
        requires_approval=bool(interrupt_on.get(item.name) or interrupt_on.get("*")),
    )


def describe_args(item: BaseTool) -> dict[str, str]:
    """提取 ``{参数名: 类型}``，用于清单展示。"""
    schema = getattr(item, "args_schema", None)
    fields = getattr(schema, "model_fields", None) or {}
    described: dict[str, str] = {}
    for name, field in fields.items():
        annotation = getattr(field, "annotation", None)
        described[name] = getattr(annotation, "__name__", str(annotation))
    return described


def find_tool(tools: Sequence[BaseTool], name: str) -> BaseTool | None:
    """按名字查工具。"""
    for item in tools:
        if item.name == name:
            return item
    return None


__all__ = [
    "BUILTIN_PROVIDERS",
    "ToolInfo",
    "ToolProvider",
    "build_tools",
    "describe_args",
    "find_tool",
    "tool_infos",
]
