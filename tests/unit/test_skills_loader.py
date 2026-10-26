"""Skill 发现与 frontmatter 校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from doudou.skills.loader import (
    SKILL_FILENAME,
    SkillError,
    discover_skills,
    load_skill,
    parse_frontmatter,
)

VALID = """\
---
name: demo-skill
description: 演示技能。当用户要求演示时使用。
custom_field: 99
---

# 正文

这里是步骤。
"""


def write_skill(root: Path, folder: str, content: str) -> Path:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SKILL_FILENAME
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- 解析


def test_parse_frontmatter() -> None:
    data, body = parse_frontmatter(VALID)

    assert data["name"] == "demo-skill"
    assert data["custom_field"] == 99
    assert body.strip().startswith("# 正文")


def test_parse_frontmatter_requires_blocks() -> None:
    with pytest.raises(SkillError, match="缺少 YAML frontmatter"):
        parse_frontmatter("# 没有 frontmatter")


def test_parse_frontmatter_rejects_non_mapping() -> None:
    with pytest.raises(SkillError, match="键值映射"):
        parse_frontmatter("---\n- a\n- b\n---\n")


def test_load_skill_ok(tmp_path: Path) -> None:
    path = write_skill(tmp_path, "demo-skill", VALID)

    info = load_skill(path)

    assert info.name == "demo-skill"
    assert info.description.startswith("演示技能")
    assert info.directory == path.parent
    assert info.extra == {"custom_field": 99}


def test_load_skill_requires_name(tmp_path: Path) -> None:
    path = write_skill(tmp_path, "x", "---\ndescription: 有描述没名字\n---\n")

    with pytest.raises(SkillError, match="缺少非空的 name"):
        load_skill(path)


def test_load_skill_requires_description(tmp_path: Path) -> None:
    path = write_skill(tmp_path, "x", "---\nname: only-name\n---\n")

    with pytest.raises(SkillError, match="缺少非空的 description"):
        load_skill(path)


def test_load_skill_rejects_bad_name(tmp_path: Path) -> None:
    """name 必须是小写短横线命名 —— 这是 Agent Skills 标准的约束。"""
    path = write_skill(tmp_path, "x", "---\nname: BadName\ndescription: 描述\n---\n")

    with pytest.raises(SkillError, match="小写短横线命名"):
        load_skill(path)


def test_load_skill_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SkillError, match="文件不存在"):
        load_skill(tmp_path / "nope" / SKILL_FILENAME)


# --------------------------------------------------------------------------- 发现


def test_discover_skills_sorted_and_recursive(tmp_path: Path) -> None:
    write_skill(tmp_path, "zeta", "---\nname: zeta\ndescription: z\n---\n")
    write_skill(tmp_path, "nested/alpha", "---\nname: alpha\ndescription: a\n---\n")

    skills, errors = discover_skills([tmp_path])

    assert errors == []
    assert [info.name for info in skills] == ["alpha", "zeta"]


def test_discover_skills_collects_errors_without_failing(tmp_path: Path) -> None:
    write_skill(tmp_path, "good", "---\nname: good\ndescription: 好的\n---\n")
    write_skill(tmp_path, "bad", "---\nname: bad\n---\n")

    skills, errors = discover_skills([tmp_path])

    assert [info.name for info in skills] == ["good"]
    assert len(errors) == 1
    assert "description" in errors[0]


def test_discover_skills_strict_raises(tmp_path: Path) -> None:
    write_skill(tmp_path, "bad", "---\nname: bad\n---\n")

    with pytest.raises(SkillError):
        discover_skills([tmp_path], strict=True)


def test_discover_skills_reports_duplicates(tmp_path: Path) -> None:
    write_skill(tmp_path / "a", "dup", "---\nname: dup\ndescription: 第一个\n---\n")
    write_skill(tmp_path / "b", "dup", "---\nname: dup\ndescription: 第二个\n---\n")

    skills, errors = discover_skills([tmp_path])

    assert len(skills) == 1
    assert len(errors) == 1
    assert "名称重复" in errors[0]


def test_discover_skills_missing_root_is_not_an_error(tmp_path: Path) -> None:
    skills, errors = discover_skills([tmp_path / "nope"])

    assert skills == []
    assert errors == []


def test_repo_skills_are_valid() -> None:
    """仓库自带的 skills/ 必须全部通过校验（否则 agent 会加载失败）。"""
    repo_skills = Path(__file__).resolve().parents[2] / "skills"

    skills, errors = discover_skills([repo_skills])

    assert errors == []
    assert {info.name for info in skills} >= {"weather"}
