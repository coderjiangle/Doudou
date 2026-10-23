"""工具注册表：内置工具装配、重名处理与清单生成。"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings
from doudou.tools.builtin import clock
from doudou.tools.registry import build_tools, describe_args, find_tool, tool_infos


def test_builtin_clock_tools_are_registered(settings: Settings) -> None:
    tools = build_tools(settings)

    assert {item.name for item in tools} == {"now", "days_between"}
    assert all(isinstance(item, BaseTool) for item in tools)


def test_clock_default_timezone_comes_from_settings(settings: Settings) -> None:
    """默认时区取自 runtime.timezone，并出现在工具描述里。"""
    tools = {item.name: item for item in build_tools(settings)}

    assert "Asia/Shanghai" in (tools["now"].description or "")


def test_now_tool_returns_local_time(settings: Settings) -> None:
    tools = {item.name: item for item in build_tools(settings)}

    result = tools["now"].invoke({"timezone": "UTC"})

    assert "UTC" in result
    assert "周" in result


def test_now_tool_falls_back_to_default_timezone(settings: Settings) -> None:
    tools = {item.name: item for item in build_tools(settings)}

    assert "CST" in tools["now"].invoke({})


def test_now_tool_rejects_unknown_timezone(settings: Settings) -> None:
    import pytest

    tools = {item.name: item for item in build_tools(settings)}

    with pytest.raises(Exception, match="无法识别的时区"):
        tools["now"].invoke({"timezone": "Mars/Olympus"})


def test_days_between(settings: Settings) -> None:
    tools = {item.name: item for item in build_tools(settings)}

    assert (
        tools["days_between"].invoke({"start_date": "2026-10-01", "end_date": "2026-10-23"})
        == "2026-10-01 到 2026-10-23 相差 22 天"
    )


def test_days_between_rejects_bad_format(settings: Settings) -> None:
    import pytest

    tools = {item.name: item for item in build_tools(settings)}

    with pytest.raises(Exception, match="日期格式错误"):
        tools["days_between"].invoke({"start_date": "10/01/2026", "end_date": "2026-10-23"})


def test_extra_tools_are_appended(settings: Settings) -> None:
    @tool
    def mcp_ping() -> str:
        """来自 MCP 的工具。"""
        return "pong"

    tools = build_tools(settings, extra=[mcp_ping])

    assert [item.name for item in tools] == ["now", "days_between", "mcp_ping"]


def test_duplicate_tool_name_keeps_first(settings: Settings) -> None:
    """重名时保留先出现的（内置优先），并丢弃后来的。"""

    @tool("now")
    def now_duplicate() -> str:
        """冒名顶替的 now。"""
        return "fake"

    tools = build_tools(settings, extra=[now_duplicate], extra_source="mcp:fake")

    assert [item.name for item in tools] == ["now", "days_between"]
    assert "获取当前的日期与时间" in (find_tool(tools, "now").description or "")


def test_tool_infos_shape(settings: Settings) -> None:
    infos = {info.name: info for info in tool_infos(settings)}

    assert set(infos) == {"now", "days_between"}
    clock_info = infos["now"]
    assert clock_info.group == "clock"
    assert clock_info.source == "builtin:clock"
    assert clock_info.args == {"timezone": "str"}
    assert clock_info.requires_approval is False
    assert clock_info.as_dict()["name"] == "now"


def test_tool_infos_marks_approval_tools(settings_factory) -> None:  # type: ignore[no-untyped-def]
    """interrupt_on 里登记的工具会被标记为需要确认。"""
    settings = settings_factory(
        config_yaml="agent:\n  interrupt_on:\n    now: true\n",
    )

    infos = {info.name: info for info in tool_infos(settings)}

    assert infos["now"].requires_approval is True
    assert infos["days_between"].requires_approval is False


def test_tool_infos_marks_approval_for_wildcard(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(config_yaml='agent:\n  interrupt_on:\n    "*": true\n')

    assert all(info.requires_approval for info in tool_infos(settings))


def test_describe_args_uses_annotations(settings: Settings) -> None:
    tools = {item.name: item for item in build_tools(settings)}

    assert describe_args(tools["days_between"]) == {"start_date": "str", "end_date": "str"}


def test_tool_infos_is_pure_and_repeatable(settings: Settings) -> None:
    """清单可重复生成：CLI 的 /tools 与 doudou tools list 都会调用它。"""
    first = tool_infos(settings)
    second = tool_infos(settings)

    assert [info.name for info in first] == [info.name for info in second]
    assert all(info.source == "builtin:clock" for info in first)


def test_providers_are_callable_without_settings_side_effects(settings: Settings) -> None:
    """多次构造互不影响：每次都是新的工具实例。"""
    first = build_tools(settings)
    second = build_tools(settings)

    assert [item.name for item in first] == [item.name for item in second]
    assert first[0] is not second[0]


def test_clock_module_exposes_provider(settings: Settings) -> None:
    assert [item.name for item in clock.build_tools(settings)] == ["now", "days_between"]
