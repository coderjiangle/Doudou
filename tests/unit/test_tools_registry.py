"""工具注册表：装配、分组开关、注解与人工确认策略。"""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from doudou.config.loader import Settings
from doudou.tools.builtin import clock
from doudou.tools.decorators import annotate, annotations_of, is_annotated
from doudou.tools.registry import (
    ALL_GROUPS,
    build_tools,
    describe_args,
    describe_groups,
    effective_interrupt_on,
    find_tool,
    group_enabled,
    requires_approval,
    tool_infos,
)

#: 无任何密钥/允许列表时应当出现的工具。
DEFAULT_TOOLS = [
    "workspace_read",
    "workspace_list",
    "note_save",
    "note_list",
    "note_read",
    "now",
    "days_between",
]


# --------------------------------------------------------------------------- 装配


def test_default_tool_set(settings: Settings) -> None:
    """默认配置下：web/http 因依赖不满足而不注册，其余全部可用。"""
    assert [item.name for item in build_tools(settings)] == DEFAULT_TOOLS


def test_clock_default_timezone_comes_from_settings(settings: Settings) -> None:
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
    tools = {item.name: item for item in build_tools(settings)}

    with pytest.raises(Exception, match="日期格式错误"):
        tools["days_between"].invoke({"start_date": "10/01/2026", "end_date": "2026-10-23"})


def test_extra_tools_are_appended(settings: Settings) -> None:
    @tool
    def mcp_ping() -> str:
        """来自 MCP 的工具。"""
        return "pong"

    tools = build_tools(settings, extra=[mcp_ping])

    assert [item.name for item in tools] == [*DEFAULT_TOOLS, "mcp_ping"]


def test_duplicate_tool_name_keeps_first(settings: Settings) -> None:
    @tool("now")
    def now_duplicate() -> str:
        """冒名顶替的 now。"""
        return "fake"

    tools = build_tools(settings, extra=[now_duplicate], extra_source="mcp:fake")

    assert [item.name for item in tools] == DEFAULT_TOOLS
    now = find_tool(tools, "now")
    assert now is not None
    assert "获取当前的日期与时间" in (now.description or "")


# --------------------------------------------------------------------------- 分组开关


def test_all_groups_declared() -> None:
    assert ALL_GROUPS == ("web", "http", "files", "notes", "skills", "clock")


def test_enabled_groups_restricts_tool_set(settings: Settings) -> None:
    restricted = _with_tools(settings, enabled_groups=["clock"])

    assert [item.name for item in build_tools(restricted)] == ["now", "days_between"]


def test_disabled_list_removes_single_tool(settings: Settings) -> None:
    restricted = _with_tools(settings, disabled=["note_save", "workspace_list"])

    names = [item.name for item in build_tools(restricted)]

    assert "note_save" not in names
    assert "workspace_list" not in names
    assert "note_read" in names


def test_disabled_tools_are_absent_from_infos(settings: Settings) -> None:
    restricted = _with_tools(settings, disabled=["note_save"])

    assert "note_save" not in {info.name for info in tool_infos(restricted)}


def test_group_enabled_helper(settings: Settings) -> None:
    assert group_enabled(settings, "clock") is True
    assert group_enabled(_with_tools(settings, enabled_groups=["clock"]), "notes") is False


# --------------------------------------------------------------------------- 分组状态（为什么不在列表里）


def test_describe_groups_explains_gating(settings: Settings) -> None:
    statuses = {status.name: status for status in describe_groups(settings)}

    assert statuses["clock"].enabled is True
    assert "TAVILY_API_KEY" in statuses["web"].reason
    assert "allowed_domains" in statuses["http"].reason


def test_describe_groups_points_at_enabled_groups(settings: Settings) -> None:
    restricted = _with_tools(settings, enabled_groups=["clock"])

    statuses = {status.name: status for status in describe_groups(restricted)}

    assert statuses["notes"].enabled is False
    assert "enabled_groups" in statuses["notes"].reason


def test_describe_groups_reports_http_enabled_with_allowlist(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(allowed_domains=("example.com",))

    statuses = {status.name: status for status in describe_groups(settings)}

    assert statuses["http"].enabled is True
    assert "http_request" in {item.name for item in build_tools(settings)}


def test_web_search_is_not_registered_without_key(settings: Settings) -> None:
    """没有凭据的工具不该占着提示词。"""
    assert "web_search" not in {item.name for item in build_tools(settings)}


def test_web_search_registered_with_key(tools_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tools_settings(env={"TAVILY_API_KEY": "tvly-test"})

    assert "web_search" in {item.name for item in build_tools(settings)}


# --------------------------------------------------------------------------- 注解


def test_builtin_tools_are_all_annotated(settings: Settings) -> None:
    """未注解的工具会被当成「有副作用」，因此内置工具必须全部显式注解。"""
    assert all(is_annotated(item) for item in build_tools(settings))


def test_only_note_save_has_side_effects(settings: Settings) -> None:
    side = {item.name for item in build_tools(settings) if annotations_of(item).side_effects}

    assert side == {"note_save"}


def test_annotate_keeps_other_metadata() -> None:
    @tool
    def sample() -> str:
        """样例。"""
        return "x"

    sample.metadata = {"other": {"keep": 1}}
    annotate(sample, group="g", side_effects=True, timeout=3.5)

    assert sample.metadata is not None
    assert sample.metadata["other"] == {"keep": 1}
    assert annotations_of(sample).group == "g"
    assert annotations_of(sample).timeout == 3.5


def test_annotations_default_to_side_effects_when_missing() -> None:
    @tool
    def unlabelled() -> str:
        """没有注解。"""
        return "x"

    assert is_annotated(unlabelled) is False
    assert annotations_of(unlabelled).side_effects is True


def test_annotations_survive_broken_metadata() -> None:
    @tool
    def broken() -> str:
        """元数据坏掉。"""
        return "x"

    broken.metadata = {"doudou": "not-a-dict"}

    assert annotations_of(broken).side_effects is True


def test_annotations_coerce_bad_timeout() -> None:
    @tool
    def weird() -> str:
        """timeout 类型不对。"""
        return "x"

    weird.metadata = {"doudou": {"side_effects": False, "timeout": "abc"}}

    assert annotations_of(weird).timeout is None
    assert annotations_of(weird).side_effects is False


# --------------------------------------------------------------------------- 人工确认策略


def test_side_effects_require_approval_by_default(settings: Settings) -> None:
    tools = {item.name: item for item in build_tools(settings)}

    assert requires_approval(settings, tools["note_save"]) is True
    assert requires_approval(settings, tools["workspace_read"]) is False
    assert requires_approval(settings, tools["now"]) is False


def test_explicit_interrupt_on_overrides_side_effect_default(settings: Settings) -> None:
    """配置能覆盖自动拦截：显式 false 表示「这个写操作不必每次确认」。"""
    relaxed = _with_agent(settings, interrupt_on={"note_save": False})
    tools = {item.name: item for item in build_tools(relaxed)}

    assert requires_approval(relaxed, tools["note_save"]) is False


def test_wildcard_interrupt_on_guards_everything(settings: Settings) -> None:
    guarded = _with_agent(settings, interrupt_on={"*": True})
    tools = {item.name: item for item in build_tools(guarded)}

    assert all(requires_approval(guarded, item) for item in tools.values())


def test_require_approval_for_side_effects_can_be_disabled(settings: Settings) -> None:
    relaxed = _with_agent(settings, require_approval_for_side_effects=False)
    tools = {item.name: item for item in build_tools(relaxed)}

    assert requires_approval(relaxed, tools["note_save"]) is False


def test_effective_interrupt_on_covers_every_tool(settings: Settings) -> None:
    """展开后的策略必须覆盖每个工具，这样「谁要确认」在执行前就是确定的。

    策略里**允许**出现工具列表之外的键（配置可以先登记还没启用的工具），
    因此这里断言的是「覆盖」而不是「相等」。
    """
    tools = build_tools(settings)
    policy = effective_interrupt_on(settings, tools)

    assert {item.name for item in tools} <= set(policy)
    assert policy["note_save"] is True
    assert policy["now"] is False


# --------------------------------------------------------------------------- 清单


def test_tool_infos_shape(settings: Settings) -> None:
    infos = {info.name: info for info in tool_infos(settings)}

    clock_info = infos["now"]
    assert clock_info.group == "clock"
    assert clock_info.source == "builtin:clock"
    assert clock_info.args == {"timezone": "str"}
    assert clock_info.side_effects is False
    assert clock_info.requires_approval is False
    assert clock_info.as_dict()["name"] == "now"

    note_info = infos["note_save"]
    assert note_info.side_effects is True
    assert note_info.requires_approval is True
    assert note_info.group == "notes"


def test_tool_infos_is_pure_and_repeatable(settings: Settings) -> None:
    first = tool_infos(settings)
    second = tool_infos(settings)

    assert [info.name for info in first] == [info.name for info in second]


def test_describe_args_uses_annotations(settings: Settings) -> None:
    tools = {item.name: item for item in build_tools(settings)}

    assert describe_args(tools["days_between"]) == {"start_date": "str", "end_date": "str"}
    assert describe_args(tools["note_save"]) == {"title": "str", "content": "str"}


def test_providers_return_fresh_instances(settings: Settings) -> None:
    first = build_tools(settings)
    second = build_tools(settings)

    assert [item.name for item in first] == [item.name for item in second]
    assert first[0] is not second[0]


def test_builtin_clock_provider_is_standalone(settings: Settings) -> None:
    assert [item.name for item in clock.build_tools(settings)] == ["now", "days_between"]


# --------------------------------------------------------------------------- 辅助


def _with_tools(settings: Settings, **updates: object) -> Settings:
    """返回 ``tools`` 段被覆盖后的新配置。

    直接改配置对象而不是回写 YAML：``Settings`` 是冻结模型，``model_copy``
    能精确表达「只有这一段不一样」，也避免测试里再走一遍 YAML 解析。
    """
    app = settings.app.model_copy(update={"tools": settings.app.tools.model_copy(update=updates)})
    return settings.model_copy(update={"app": app})


def _with_agent(settings: Settings, **updates: object) -> Settings:
    """返回 ``agent`` 段被覆盖后的新配置。"""
    app = settings.app.model_copy(update={"agent": settings.app.agent.model_copy(update=updates)})
    return settings.model_copy(update={"app": app})
