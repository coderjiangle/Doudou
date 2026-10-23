"""Agent Skills package: discovery and indexing."""

from __future__ import annotations

from doudou.skills.loader import (
    SKILL_FILENAME,
    SkillError,
    SkillInfo,
    discover_skills,
    load_skill,
    parse_frontmatter,
)

__all__ = [
    "SKILL_FILENAME",
    "SkillError",
    "SkillInfo",
    "discover_skills",
    "load_skill",
    "parse_frontmatter",
]
