"""Agent Skills package: discovery, indexing and on-demand loading."""

from __future__ import annotations

from doudou.skills.loader import (
    SKILL_FILENAME,
    SkillError,
    SkillInfo,
    discover_skills,
    load_skill,
    parse_frontmatter,
)
from doudou.skills.registry import SkillRegistry

__all__ = [
    "SKILL_FILENAME",
    "SkillError",
    "SkillInfo",
    "SkillRegistry",
    "discover_skills",
    "load_skill",
    "parse_frontmatter",
]
