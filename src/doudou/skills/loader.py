"""Discover SKILL.md directories and validate frontmatter."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from doudou.observability.logging import get_logger

logger = get_logger(__name__)

SKILL_FILENAME = "SKILL.md"

_FRONTMATTER = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.DOTALL)
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillError(ValueError):
    """Skill 目录或 frontmatter 不合法。"""


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """一个已发现的 Skill。"""

    name: str
    description: str
    path: Path
    #: 完整正文（frontmatter 之后的部分）。
    body: str = ""
    #: frontmatter 中除 ``name`` / ``description`` 外的其它字段。
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return self.path.parent


def parse_frontmatter(text: str, *, source: Path | None = None) -> tuple[dict[str, Any], str]:
    """拆分 ``SKILL.md`` 的 frontmatter 与正文。"""
    match = _FRONTMATTER.match(text)
    if match is None:
        where = f"（{source}）" if source else ""
        raise SkillError(f"缺少 YAML frontmatter{where}：文件必须以 '---' 开头并以 '---' 结束。")
    raw = match.group(1)
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"frontmatter 不是合法 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise SkillError("frontmatter 必须是键值映射。")
    return data, text[match.end() :]


def load_skill(path: Path) -> SkillInfo:
    """读取并校验单个 ``SKILL.md``。"""
    if not path.is_file():
        raise SkillError(f"文件不存在：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"读取失败：{path}（{exc}）") from exc

    data, body = parse_frontmatter(text, source=path)

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillError(f"{path}：frontmatter 缺少非空的 name。")
    name = name.strip()
    if not _NAME_PATTERN.match(name):
        raise SkillError(f"{path}：name={name!r} 不合法，要求小写短横线命名（如 example-echo）。")

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(
            f"{path}：frontmatter 缺少非空的 description。"
            "描述决定模型何时加载该 Skill，请写清「做什么 + 什么时候用」。"
        )

    extra = {key: value for key, value in data.items() if key not in {"name", "description"}}

    return SkillInfo(
        name=name,
        description=description.strip(),
        path=path,
        body=body.strip(),
        extra=extra,
    )


def discover_skills(
    roots: Iterable[Path],
    *,
    strict: bool = False,
) -> tuple[list[SkillInfo], list[str]]:
    """扫描若干根目录，返回 ``(已发现的 Skill, 错误列表)``。

    Args:
        roots: 根目录列表；目录本身或其后代含有 ``SKILL.md`` 均会被发现，
            但不会深入已经命中的 Skill 目录。
        strict: ``True`` 时遇到非法 Skill 直接抛 :class:`SkillError`。

    Returns:
        按 ``name`` 排序的 Skill 列表，以及所有校验/读取错误的消息。
    """
    found: dict[str, SkillInfo] = {}
    errors: list[str] = []

    for root in roots:
        if not root.is_dir():
            logger.debug("skills.root_missing", root=str(root))
            continue
        for skill_file in sorted(root.rglob(SKILL_FILENAME)):
            try:
                info = load_skill(skill_file)
            except SkillError as exc:
                if strict:
                    raise
                errors.append(str(exc))
                logger.warning("skills.invalid", path=str(skill_file), error=str(exc))
                continue
            existing = found.get(info.name)
            if existing is not None:
                message = (
                    f"Skill 名称重复：{info.name}（{existing.path} 与 {info.path}），保留前者。"
                )
                errors.append(message)
                logger.warning("skills.duplicate_name", name=info.name, kept=str(existing.path))
                continue
            found[info.name] = info

    return [found[name] for name in sorted(found)], errors


__all__ = [
    "SKILL_FILENAME",
    "SkillError",
    "SkillInfo",
    "discover_skills",
    "load_skill",
    "parse_frontmatter",
]
