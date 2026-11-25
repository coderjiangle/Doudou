"""Skill 索引：发现、检索、附属文件访问与重载。"""

from __future__ import annotations

from pathlib import Path

import pytest

from doudou.skills.loader import SkillError
from doudou.skills.registry import SkillRegistry
from tests.conftest import DEMO_SKILL

SECOND_SKILL = """\
---
name: research-like
description: 结构化调研工作流。当用户要求调研或对比方案时使用。
---

# 调研

先界定问题，再检索。
"""


@pytest.fixture
def registry(skills_settings) -> SkillRegistry:  # type: ignore[no-untyped-def]
    settings = skills_settings(
        skills={"demo-skill": DEMO_SKILL, "research-like": SECOND_SKILL},
        extra_files={"demo-skill/reference.md": "细节内容"},
    )
    return SkillRegistry.from_settings(settings)


# --------------------------------------------------------------------------- 发现


def test_discover_and_index(registry: SkillRegistry) -> None:
    assert registry.names == ["demo-skill", "research-like"]
    assert len(registry) == 2
    assert registry.errors == []


def test_get_by_name(registry: SkillRegistry) -> None:
    skill = registry.get("demo-skill")

    assert skill is not None
    assert skill.description.startswith("演示技能")
    assert "按需加载" in skill.body
    assert registry.get("不存在") is None


def test_get_strips_whitespace(registry: SkillRegistry) -> None:
    assert registry.get("  demo-skill  ") is not None


def test_empty_registry(tmp_path: Path) -> None:
    empty = SkillRegistry([tmp_path / "nope"])

    assert len(empty) == 0
    assert empty.names == []
    assert empty.errors == []


def test_invalid_skill_is_reported_not_raised(skills_settings) -> None:  # type: ignore[no-untyped-def]
    """一个写坏的 SKILL.md 不该让服务起不来，但必须被记下来。"""
    settings = skills_settings(
        skills={"good": DEMO_SKILL.replace("demo-skill", "good"), "bad": "---\nname: bad\n---\n"}
    )

    registry = SkillRegistry.from_settings(settings)

    assert registry.names == ["good"]
    assert len(registry.errors) == 1
    assert "description" in registry.errors[0]


def test_reload_picks_up_new_skills(skills_settings) -> None:  # type: ignore[no-untyped-def]
    """``reload`` 是就地刷新：持有它的工具与提示词不需要重建。"""
    settings = skills_settings()
    registry = SkillRegistry.from_settings(settings)
    assert registry.names == ["demo-skill"]

    root = settings.skills_dirs[0]
    new_dir = root / "added"
    new_dir.mkdir()
    (new_dir / "SKILL.md").write_text(
        SECOND_SKILL.replace("research-like", "added"), encoding="utf-8"
    )

    count = registry.reload()

    assert count == 2
    assert registry.get("added") is not None


# --------------------------------------------------------------------------- 检索


def test_find_matches_name_with_higher_weight(registry: SkillRegistry) -> None:
    results = registry.find("research")

    assert results[0].name == "research-like"


def test_find_matches_chinese_description(registry: SkillRegistry) -> None:
    results = registry.find("调研")

    assert [skill.name for skill in results] == ["research-like"]


def test_find_returns_empty_for_no_match(registry: SkillRegistry) -> None:
    assert registry.find("zzzz不存在的词") == []


def test_find_empty_query_returns_all(registry: SkillRegistry) -> None:
    assert len(registry.find("")) == 2


def test_find_respects_limit(registry: SkillRegistry) -> None:
    assert len(registry.find("", limit=1)) == 1


# --------------------------------------------------------------------------- 附属文件


def test_list_files_excludes_skill_md(registry: SkillRegistry) -> None:
    assert registry.list_files("demo-skill") == ["reference.md"]
    assert registry.list_files("research-like") == []


def test_list_files_unknown_skill(registry: SkillRegistry) -> None:
    with pytest.raises(SkillError, match="没有名为"):
        registry.list_files("nope")


def test_read_file(registry: SkillRegistry) -> None:
    assert registry.read_file("demo-skill", "reference.md") == "细节内容"


def test_read_file_unknown_skill(registry: SkillRegistry) -> None:
    with pytest.raises(SkillError, match="可用：demo-skill"):
        registry.read_file("nope", "x.md")


def test_read_file_missing_lists_alternatives(registry: SkillRegistry) -> None:
    with pytest.raises(SkillError, match="reference"):
        registry.read_file("demo-skill", "nope.md")


@pytest.mark.parametrize("relative", ["../outside.md", "..", "./../../x"])
def test_read_file_rejects_traversal(registry: SkillRegistry, relative: str) -> None:
    with pytest.raises(SkillError, match="之外"):
        registry.read_file("demo-skill", relative)


def test_read_file_rejects_absolute(registry: SkillRegistry) -> None:
    with pytest.raises(SkillError, match="相对 Skill 目录"):
        registry.read_file("demo-skill", "C:/Windows/win.ini")


def test_read_file_rejects_empty(registry: SkillRegistry) -> None:
    with pytest.raises(SkillError, match="不能为空"):
        registry.read_file("demo-skill", "   ")


def test_read_file_rejects_symlink_escape(tmp_path: Path, skills_settings) -> None:  # type: ignore[no-untyped-def]
    """字符串里没有 ``..`` 也可能跑出去 —— 靠 resolve() 之后比较才拦得住。"""
    outside = tmp_path / "outside.md"
    outside.write_text("工作区外的内容", encoding="utf-8")
    settings = skills_settings(extra_files={"demo-skill/link.md": "占位"})
    link = settings.skills_dirs[0] / "demo-skill" / "link.md"
    link.unlink()
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")

    registry = SkillRegistry.from_settings(settings)

    with pytest.raises(SkillError, match="之外"):
        registry.read_file("demo-skill", "link.md")


def test_read_file_refuses_huge_file(skills_settings) -> None:  # type: ignore[no-untyped-def]
    """超大文件在读之前就被挡掉 —— 截断发生在读完之后。"""
    settings = skills_settings(extra_files={"demo-skill/big.md": "x" * (20_000 * 10)})
    registry = SkillRegistry.from_settings(settings)

    with pytest.raises(SkillError, match="超过单次读取上限"):
        registry.read_file("demo-skill", "big.md")


def test_read_file_rejects_non_utf8(skills_settings) -> None:  # type: ignore[no-untyped-def]
    settings = skills_settings()
    (settings.skills_dirs[0] / "demo-skill" / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
    registry = SkillRegistry.from_settings(settings)

    with pytest.raises(SkillError, match="不是 UTF-8"):
        registry.read_file("demo-skill", "bin.dat")


# --------------------------------------------------------------------------- 快照


def test_describe_shape(registry: SkillRegistry) -> None:
    entries = {entry.name: entry for entry in registry.describe()}

    entry = entries["demo-skill"]
    assert entry.body_chars > 0
    assert entry.files == ["reference.md"]
    assert entry.directory.endswith("demo-skill")
    assert entry.as_dict()["name"] == "demo-skill"
