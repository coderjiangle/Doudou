"""Index skills by name, description and source path.

:class:`SkillRegistry` 是 Skill 的**唯一入口**：发现、按名查找、按描述检索、
读附属文件。三个消费方共用它：

* 系统提示词只注入**索引**（name + description），正文不进上下文预算；
* ``read_skill`` 工具在被调用时读全文 —— 这就是渐进式披露；
* CLI 与 ``GET /skills`` 用它做检视。

**自研，不引入 deepagents / LangGraph**：它们的 ``SkillsMiddleware`` 做的是
同一件事（启动只读 frontmatter、按需读全文），我们用「索引注入 + ``read_skill`` 工具」
就能覆盖，而且不必为此背上一整套运行时与事件映射层。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.skills.loader import SKILL_FILENAME, SkillError, SkillInfo, discover_skills
from doudou.utils.text import truncate

logger = get_logger(__name__)

#: 单次读取附属文件的上限。
DEFAULT_MAX_FILE_CHARS = 20000

#: 拒绝读取超过「上限 × 这个倍数」的文件 —— 截断发生在读完之后，
#: 所以必须先看大小，否则一个 GB 级文件就能把内存吃光。
SIZE_GUARD_FACTOR = 8

_WORD = re.compile(r"[a-zA-Z0-9_]+")


@dataclass(slots=True)
class SkillOverview:
    """一个 Skill 的快照，给 CLI / API / JSON 共用。

    与 MCP 的 ``ServerOverview`` 同理：强类型而不是 ``dict[str, object]``，
    否则每个消费方都要在 ``object`` 上做断言。
    """

    name: str
    description: str
    path: str
    directory: str
    body_chars: int = 0
    files: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "directory": self.directory,
            "body_chars": self.body_chars,
            "files": list(self.files),
            "extra": dict(self.extra),
        }


class SkillRegistry:
    """已发现 Skill 的索引。

    可变对象：``reload()`` 会就地刷新内容，因此持有它的工具与提示词
    **不需要重建**（工具在调用时按名查找，提示词由 Agent 在重载后更新）。
    """

    def __init__(
        self,
        roots: Sequence[Path],
        *,
        max_file_chars: int = DEFAULT_MAX_FILE_CHARS,
    ) -> None:
        self.roots = tuple(roots)
        self.max_file_chars = max_file_chars
        self._skills: list[SkillInfo] = []
        self._by_name: dict[str, SkillInfo] = {}
        self._errors: list[str] = []
        self.reload()

    @classmethod
    def from_settings(cls, settings: Settings) -> SkillRegistry:
        """按配置里的 ``agent.skills`` 构造。"""
        return cls(settings.skills_dirs)

    # ---------------------------------------------------------------- 发现

    def reload(self) -> int:
        """重新扫描根目录，返回发现的 Skill 数量。

        发现过程中的错误**不抛异常**：一个写坏的 SKILL.md 不该让服务起不来，
        但必须记下来并暴露给 ``skills check`` —— 静默跳过会让人以为技能已生效。
        """
        skills, errors = discover_skills(self.roots)
        self._skills = skills
        self._by_name = {skill.name: skill for skill in skills}
        self._errors = errors
        if errors:
            logger.warning("skills.load_errors", count=len(errors), errors=errors[:5])
        logger.info(
            "skills.reloaded",
            count=len(skills),
            names=[skill.name for skill in skills],
            roots=[str(root) for root in self.roots],
        )
        return len(skills)

    # ---------------------------------------------------------------- 查询

    @property
    def skills(self) -> list[SkillInfo]:
        """按名字排序的全部 Skill。"""
        return list(self._skills)

    @property
    def errors(self) -> list[str]:
        """发现过程中被跳过的 Skill 及原因。"""
        return list(self._errors)

    @property
    def names(self) -> list[str]:
        return [skill.name for skill in self._skills]

    def __len__(self) -> int:
        return len(self._skills)

    def get(self, name: str) -> SkillInfo | None:
        return self._by_name.get(name.strip())

    def find(self, query: str, *, limit: int = 5) -> list[SkillInfo]:
        """按关键词粗检索。

        Skill 数量少时模型直接看索引就够了；这个接口服务于**技能很多**的场景
        （索引会被截断，此时需要一条「按描述找」的路径），以及 CLI 的搜索。
        故意做得很简单：命中名字权重高、命中描述权重低，不做语义检索。
        """
        terms = [term.casefold() for term in _WORD.findall(query)]
        terms.extend(char for char in query if "\u4e00" <= char <= "\u9fff")
        if not terms:
            return self.skills[:limit]

        scored: list[tuple[int, SkillInfo]] = []
        for skill in self._skills:
            name = skill.name.casefold()
            description = skill.description.casefold()
            score = 0
            for term in terms:
                if term in name:
                    score += 3
                elif term in description:
                    score += 1
            if score:
                scored.append((score, skill))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [skill for _score, skill in scored[:limit]]

    # ---------------------------------------------------------------- 附属文件

    def list_files(self, name: str) -> list[str]:
        """列出 Skill 目录下的附属文件（相对路径，不含 SKILL.md）。"""
        skill = self.get(name)
        if skill is None:
            raise SkillError(f"没有名为 {name!r} 的 Skill。")
        files = [
            item.relative_to(skill.directory).as_posix()
            for item in sorted(skill.directory.rglob("*"))
            if item.is_file() and item.name != SKILL_FILENAME
        ]
        return files

    def read_file(self, name: str, relative: str) -> str:
        """读取 Skill 目录下的附属文件。

        ``resolve()`` 之后再确认仍在 Skill 目录内 —— 只检查字符串里有没有 ``..``
        是挡不住软链接的（同一个理由见 ``tools/builtin/files.py``）。
        """
        skill = self.get(name)
        if skill is None:
            available = "、".join(self.names) or "（无）"
            raise SkillError(f"没有名为 {name!r} 的 Skill。可用：{available}")

        target = self._resolve(skill, relative)
        if not target.is_file():
            listed = "、".join(self.list_files(name)) or "（没有附属文件）"
            raise SkillError(f"Skill {name!r} 里没有文件 {relative!r}。现有：{listed}")

        size = target.stat().st_size
        guard = self.max_file_chars * SIZE_GUARD_FACTOR
        if size > guard:
            raise SkillError(f"文件 {relative!r} 有 {size} 字节，超过单次读取上限，已拒绝加载。")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillError(f"文件 {relative!r} 不是 UTF-8 文本。") from exc
        except OSError as exc:
            raise SkillError(f"读取 {relative!r} 失败：{exc}") from exc

        logger.info("skills.file_read", skill=name, path=relative, bytes=size)
        return truncate(text.strip(), self.max_file_chars)

    @staticmethod
    def _resolve(skill: SkillInfo, relative: str) -> Path:
        if not relative.strip():
            raise SkillError("文件路径不能为空。")
        candidate = Path(relative.strip().replace("\\", "/"))
        if candidate.is_absolute():
            raise SkillError("请给相对 Skill 目录的路径，不要用绝对路径。")
        resolved = (skill.directory / candidate).resolve()
        if resolved != skill.directory and not resolved.is_relative_to(skill.directory):
            raise SkillError(f"路径 {relative!r} 指向 Skill 目录之外，已拒绝访问。")
        return resolved

    # ---------------------------------------------------------------- 快照

    def describe(self) -> list[SkillOverview]:
        """给 ``GET /skills`` 与 CLI 用的结构化快照。"""
        return [
            SkillOverview(
                name=skill.name,
                description=skill.description,
                path=str(skill.path),
                directory=str(skill.directory),
                body_chars=len(skill.body),
                files=self.list_files(skill.name),
                extra=dict(skill.extra),
            )
            for skill in self._skills
        ]


__all__ = [
    "DEFAULT_MAX_FILE_CHARS",
    "SIZE_GUARD_FACTOR",
    "SkillOverview",
    "SkillRegistry",
]
