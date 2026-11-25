"""渐进式披露的两半：提示词只注入索引，正文由工具按需读取。"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from doudou.agent.prompts import load_system_prompt, render_skill_index
from doudou.agent.react import ReactAgent
from doudou.agent.testing import ScriptedChatModel, tool_call
from doudou.config.loader import Settings
from doudou.skills.loader import SkillInfo
from doudou.skills.registry import SkillRegistry
from doudou.tools.builtin import skills as skills_tools
from tests.conftest import DEMO_SKILL


def make_skill(name: str, description: str, body: str = "正文内容") -> SkillInfo:
    from pathlib import Path

    return SkillInfo(
        name=name, description=description, path=Path(f"/x/{name}/SKILL.md"), body=body
    )


# --------------------------------------------------------------------------- 提示词索引


def test_index_contains_name_and_description() -> None:
    index = render_skill_index([make_skill("alpha", "做某事。当需要做某事时使用。")])

    assert "alpha" in index
    assert "做某事" in index


def test_index_does_not_leak_body() -> None:
    """正文**不进**上下文预算。"""
    index = render_skill_index([make_skill("alpha", "描述", body="机密正文内容")])

    assert "机密正文内容" not in index


def test_index_explains_on_demand_loading() -> None:
    index = render_skill_index([make_skill("alpha", "描述")])

    assert "read_skill" in index
    # 必须明确告诉模型「无关的技能不要读」，否则它会把所有技能都拉一遍
    assert "无关" in index


def test_index_empty_for_no_skills() -> None:
    assert render_skill_index([]) == ""


def test_index_truncates_and_says_how_many_left() -> None:
    skills = [make_skill(f"s{index}", "描述") for index in range(10)]

    index = render_skill_index(skills, limit=3)

    assert index.count("- `s") == 3
    assert "另有 7 个技能未列出" in index


def test_system_prompt_includes_skill_index(skills_settings) -> None:  # type: ignore[no-untyped-def]
    settings = skills_settings()
    registry = SkillRegistry.from_settings(settings)

    prompt = load_system_prompt(settings, skills=registry.skills)

    assert "## 可用技能" in prompt
    assert "demo-skill" in prompt
    assert "按需加载" not in prompt  # 正文不该出现


def test_system_prompt_without_skills(skills_settings) -> None:  # type: ignore[no-untyped-def]
    settings = skills_settings(skills={})

    prompt = load_system_prompt(settings, skills=())

    assert "## 可用技能" not in prompt


def test_system_prompt_respects_memory_and_skills_slots(skills_settings) -> None:  # type: ignore[no-untyped-def]
    """模板显式声明 {{ memory }} / {{ skills }} 时按槽位插入，末尾人设不被挤到后面。"""
    settings = skills_settings()
    assert settings.system_prompt_path is not None
    settings.system_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    settings.system_prompt_path.write_text(
        "头部人设\n\n{{ memory }}\n\n{{ skills }}\n\n尾部口吻示例",
        encoding="utf-8",
    )
    registry = SkillRegistry.from_settings(settings)

    prompt = load_system_prompt(settings, skills=registry.skills, memory="## 长期记忆\n- 乐哥")

    assert prompt.startswith("头部人设")
    assert prompt.endswith("尾部口吻示例")
    assert prompt.count("## 长期记忆") == 1
    assert prompt.count("## 可用技能") == 1


def test_system_prompt_includes_runtime_environment(
    settings_factory,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内置模板与自定义无槽位模板都应自动带上当前操作系统与默认 Shell。"""
    import sys

    settings = settings_factory(config_yaml="agent:\n  system_prompt_file: null\n")
    monkeypatch.setattr(sys, "platform", "win32")
    win_prompt = load_system_prompt(settings)
    assert "## 当前执行环境" in win_prompt
    assert "Windows" in win_prompt
    assert "cmd.exe" in win_prompt

    monkeypatch.setattr(sys, "platform", "linux")
    linux_prompt = load_system_prompt(settings)
    assert "## 当前执行环境" in linux_prompt
    assert "/bin/sh" in linux_prompt


# --------------------------------------------------------------------------- 工具注册


def test_no_tools_without_skills(settings: Settings) -> None:
    """没有技能时不给模型一个永远失败的 read_skill。"""
    assert skills_tools.build_tools(settings) == []


def test_tools_registered_with_skills(skills_settings) -> None:  # type: ignore[no-untyped-def]
    settings = skills_settings()

    names = [tool.name for tool in skills_tools.build_tools(settings)]

    assert names == ["read_skill", "list_skill_files", "skill_read_file"]


def test_skill_tools_are_read_only(skills_settings) -> None:  # type: ignore[no-untyped-def]
    from doudou.tools.decorators import annotations_of

    settings = skills_settings()
    tools = skills_tools.build_tools(settings)

    assert all(annotations_of(tool).side_effects is False for tool in tools)
    assert all(annotations_of(tool).group == "skills" for tool in tools)


def test_registry_is_shared_between_prompt_and_tools(skills_settings) -> None:  # type: ignore[no-untyped-def]
    """提示词里列的技能与 read_skill 能读到的必须来自同一份索引。"""
    from doudou.agent.factory import build_agent

    settings = skills_settings()
    bundle = build_agent(settings, model=ScriptedChatModel(steps=[]))

    assert "demo-skill" in bundle.system_prompt
    assert "read_skill" in bundle.tool_names
    assert bundle.skills is not None
    assert bundle.skills.names == ["demo-skill"]


# --------------------------------------------------------------------------- 工具行为


def test_read_skill_returns_body(skills_settings) -> None:  # type: ignore[no-untyped-def]
    settings = skills_settings()
    tools = {tool.name: tool for tool in skills_tools.build_tools(settings)}

    text = tools["read_skill"].invoke({"name": "demo-skill"})

    assert "# 技能：demo-skill" in text
    assert "按需加载" in text


def test_read_skill_mentions_attachments(skills_settings) -> None:  # type: ignore[no-untyped-def]
    settings = skills_settings(extra_files={"demo-skill/reference.md": "细节"})
    tools = {tool.name: tool for tool in skills_tools.build_tools(settings)}

    text = tools["read_skill"].invoke({"name": "demo-skill"})

    assert "reference.md" in text
    assert "skill_read_file" in text


def test_read_skill_unknown_lists_available(skills_settings) -> None:  # type: ignore[no-untyped-def]
    import pytest

    settings = skills_settings()
    tools = {tool.name: tool for tool in skills_tools.build_tools(settings)}

    with pytest.raises(Exception, match="可用：demo-skill"):
        tools["read_skill"].invoke({"name": "nope"})


def test_list_skill_files(skills_settings) -> None:  # type: ignore[no-untyped-def]
    settings = skills_settings(extra_files={"demo-skill/templates/report.md": "模板"})
    tools = {tool.name: tool for tool in skills_tools.build_tools(settings)}

    assert "templates/report.md" in tools["list_skill_files"].invoke({"name": "demo-skill"})


def test_list_skill_files_when_none(skills_settings) -> None:  # type: ignore[no-untyped-def]
    settings = skills_settings()
    tools = {tool.name: tool for tool in skills_tools.build_tools(settings)}

    assert "没有附属文件" in tools["list_skill_files"].invoke({"name": "demo-skill"})


def test_skill_read_file(skills_settings) -> None:  # type: ignore[no-untyped-def]
    settings = skills_settings(extra_files={"demo-skill/reference.md": "参考细节"})
    tools = {tool.name: tool for tool in skills_tools.build_tools(settings)}

    assert (
        tools["skill_read_file"].invoke({"name": "demo-skill", "path": "reference.md"})
        == "参考细节"
    )


# --------------------------------------------------------------------------- 端到端


async def test_agent_loads_skill_on_demand(skills_settings) -> None:  # type: ignore[no-untyped-def]
    """完整链路：模型从索引里看到技能 → 调 read_skill → 按内容作答。"""
    tools: list[BaseTool] = list(skills_tools.build_tools(skills_settings()))
    agent = ReactAgent(
        model=ScriptedChatModel(
            steps=[
                tool_call("read_skill", {"name": "demo-skill"}, call_id="c1"),
                # 第二轮把读到的内容复述出来，证明它真的进了上下文
                lambda messages: __import__(
                    "langchain_core.messages", fromlist=["AIMessage"]
                ).AIMessage(
                    content=[
                        {"type": "text", "text": f"我读到了技能内容：{'按需加载' in str(messages)}"}
                    ]
                ),
            ]
        ),
        tools=tools,
        system_prompt="",
    )

    result = await agent.ainvoke([HumanMessage(content="演示一下技能机制")])

    assert "read_skill" in [event.tool_name for event in result.events if event.type == "tool_call"]
    tool_messages = [m for m in result.messages if m.type == "tool"]
    assert tool_messages
    assert "按需加载" in str(tool_messages[0].content)


def test_bundle_refresh_skills_updates_prompt(skills_settings) -> None:  # type: ignore[no-untyped-def]
    """热重载：新加一个技能后，提示词必须跟着变。"""
    from doudou.agent.factory import build_agent

    settings = skills_settings()
    bundle = build_agent(settings, model=ScriptedChatModel(steps=[]))
    before = bundle.system_prompt
    assert "added-skill" not in before

    root = settings.skills_dirs[0]
    (root / "added-skill").mkdir()
    (root / "added-skill" / "SKILL.md").write_text(
        DEMO_SKILL.replace("demo-skill", "added-skill"), encoding="utf-8"
    )

    count = bundle.refresh_skills()

    assert count == 2
    assert "added-skill" in bundle.system_prompt
    assert bundle.system_prompt != before
    # Agent 用的也必须是新提示词，否则「热重载」只是看着变了
    assert bundle.agent.system_prompt == bundle.system_prompt


def test_bundle_refresh_without_skills_is_safe() -> None:
    """没有索引时刷新不该炸。"""
    from doudou.agent.factory import AgentBundle

    assert (
        AgentBundle(
            settings=None,  # type: ignore[arg-type]
            agent=ReactAgent(model=ScriptedChatModel(steps=[]), system_prompt=""),
        ).refresh_skills()
        == 0
    )
