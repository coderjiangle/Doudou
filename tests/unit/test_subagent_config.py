"""子 Agent 的独立模型、技能索引与工具白名单必须兑现配置。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from doudou.agent.subagents import SubAgentRegistry, SubAgentSpec, select_tools
from doudou.agent.testing import ScriptedChatModel, summarize_last_tool_result, tool_call
from doudou.config.loader import Settings
from doudou.skills.registry import SkillRegistry
from doudou.tools.builtin import clock, delegation, skills


@pytest.fixture
def child_settings(
    settings_factory: Callable[..., Settings],
) -> Callable[..., Settings]:
    def make(spec: dict[str, Any], *, tools_config: str = "") -> Settings:
        return settings_factory(
            config_yaml=(
                f"agent:\n  subagents: {json.dumps([spec], ensure_ascii=False)}\n{tools_config}"
            ),
            use_env=False,
        )

    return make


async def test_explicit_model_and_empty_tools_override_the_parent(
    child_settings: Callable[..., Settings],
) -> None:
    settings = child_settings(
        {
            "name": "planner",
            "description": "plan",
            "prompt": "Only plan",
            "model": "scripted:echo:child answer",
            "tools": [],
        }
    )
    task = delegation.build_tools(
        settings,
        model=ScriptedChatModel(steps=[AIMessage(content="parent answer")]),
        available_tools=clock.build_tools(settings),
    )[0]
    result = await task.ainvoke({"description": "plan"})
    assert "child answer" in result
    assert "parent answer" not in result


def test_explicit_empty_whitelist_disables_tools_but_omission_inherits(settings: Settings) -> None:
    available = clock.build_tools(settings)
    assert select_tools(SubAgentSpec(name="a", description="d", tools=[]), available) == []
    assert select_tools(SubAgentSpec(name="a", description="d"), available) == list(available)


def test_unavailable_child_model_is_not_registered(child_settings: Callable[..., Settings]) -> None:
    settings = child_settings(
        {"name": "bad", "description": "d", "prompt": "p", "model": "scripted:missing"}
    )
    assert delegation.build_tools(settings, model=ScriptedChatModel()) == []


async def test_child_skill_paths_are_relative_to_agents_file_and_isolated(
    child_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    directory = tmp_path / "child-skills" / "recipe"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: recipe\ndescription: Child recipe\n---\n\nPRIVATE CHILD BODY", encoding="utf-8"
    )
    parent = tmp_path / "parent-skills" / "parent"
    parent.mkdir(parents=True)
    (parent / "SKILL.md").write_text(
        "---\nname: parent\ndescription: Parent only\n---\n\nPRIVATE PARENT BODY", encoding="utf-8"
    )
    prompts: list[str] = []

    def ask_for_skill(messages: list[BaseMessage]) -> AIMessage:
        prompts.extend(
            str(message.content) for message in messages if isinstance(message, SystemMessage)
        )
        return tool_call("read_skill", {"name": "recipe"})

    settings = child_settings(
        {
            "name": "cook",
            "description": "cook",
            "prompt": "Child instructions",
            "skills": ["../child-skills"],
            "tools": ["read_skill"],
        }
    )
    task = delegation.build_tools(
        settings,
        model=ScriptedChatModel(steps=[ask_for_skill, summarize_last_tool_result()]),
        available_tools=skills.build_tools(settings, registry=SkillRegistry([parent.parent])),
    )[0]
    result = await task.ainvoke({"description": "cook"})
    assert "PRIVATE CHILD BODY" in result
    assert "PRIVATE PARENT BODY" not in result
    assert prompts and "recipe" in prompts[0]
    assert "PRIVATE CHILD BODY" not in prompts[0]
    assert "Parent only" not in prompts[0]


@pytest.mark.parametrize(
    "spec_tools,tools_config",
    [
        ([], ""),
        (["read_skill"], "tools:\n  disabled: [read_skill]\n"),
        (["read_skill"], "tools:\n  enabled_groups: [clock]\n"),
    ],
)
async def test_child_skills_do_not_bypass_tool_policy(
    child_settings: Callable[..., Settings],
    tmp_path: Path,
    spec_tools: list[str],
    tools_config: str,
) -> None:
    directory = tmp_path / "child-skills" / "recipe"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: recipe\ndescription: recipe\n---\n\nSECRET BODY", encoding="utf-8"
    )
    settings = child_settings(
        {
            "name": "cook",
            "description": "d",
            "prompt": "p",
            "skills": ["../child-skills"],
            "tools": spec_tools,
        },
        tools_config=tools_config,
    )
    task = delegation.build_tools(
        settings,
        model=ScriptedChatModel(
            steps=[tool_call("read_skill", {"name": "recipe"}), summarize_last_tool_result()]
        ),
    )[0]
    result = await task.ainvoke({"description": "cook"})
    assert "SECRET BODY" not in result
    assert "不存在" in result


async def test_child_inherits_fallback_models(child_settings: Callable[..., Settings]) -> None:
    def fail(_messages: list[BaseMessage]) -> AIMessage:
        raise RuntimeError("unavailable")

    settings = child_settings({"name": "helper", "description": "d", "prompt": "p", "tools": []})
    task = delegation.build_tools(
        settings,
        model=ScriptedChatModel(steps=[fail]),
        fallback_models=[ScriptedChatModel(steps=[AIMessage(content="backup answer")])],
    )[0]
    assert "backup answer" in await task.ainvoke({"description": "help"})


def test_registry_distinguishes_inherited_and_disabled_tools() -> None:
    registry = SubAgentRegistry(
        [
            SubAgentSpec(name="inherit", description="d"),
            SubAgentSpec(name="disabled", description="d", tools=[]),
        ]
    )
    assert [spec["tools"] for spec in registry.describe()] == [None, []]
