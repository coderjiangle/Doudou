"""Skill 的按需加载工具（渐进式披露的另一半）。

系统提示词里只有技能的**索引**（name + description）；正文与附属文件由这里的
工具在模型判断需要时才读进来。这是 M6 的核心：

* 启动上下文只随「技能数量」增长，而不随「技能体积」增长；
* 模型自己判断该不该加载，不需要人预先挑好。

Skill 数量为 0 时**不注册任何工具** —— 与「依赖不满足就整组不注册」保持一致，
避免给模型一个永远失败的 `read_skill`。
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.skills.loader import SkillError
from doudou.skills.registry import SkillRegistry
from doudou.tools.decorators import annotate

logger = get_logger(__name__)

GROUP = "skills"

#: ``read_skill`` 返回的正文上限（防护性上限，正常 Skill 远小于此）。
MAX_BODY_CHARS = 40000


def build_tools(
    settings: Settings,
    *,
    registry: SkillRegistry | None = None,
) -> Sequence[BaseTool]:
    """构造 Skill 工具；没有可用的 Skill 时返回空列表。

    Args:
        settings: 用于定位 Skill 根目录。
        registry: 复用已有索引（Agent 装配时传入，保证与提示词用的是同一份）。
    """
    skills = registry if registry is not None else SkillRegistry.from_settings(settings)
    if not len(skills):
        logger.debug("tools.skills_skipped", reason="no skills discovered")
        return []

    @tool
    def read_skill(name: str) -> str:
        """读取某个技能的完整内容。

        Args:
            name: 技能名，见系统提示词里「可用技能」一节。

        Returns:
            该技能 SKILL.md 的完整正文（含步骤与注意事项）；
            技能不存在时会列出所有可用的技能名。

        只有在**确实需要该技能的做法**时才调用本工具 —— 它会把整篇正文
        加入上下文。与当前任务无关的技能不要读。
        """
        skill = skills.get(name)
        if skill is None:
            available = "、".join(skills.names) or "（无）"
            raise SkillError(f"没有名为 {name!r} 的技能。可用：{available}")
        body = skill.body.strip()
        if len(body) > MAX_BODY_CHARS:
            body = f"{body[:MAX_BODY_CHARS]}\n\n（内容过长已截断）"
        logger.info("tools.skill_read", skill=skill.name, chars=len(body))
        attachments = skills.list_files(skill.name)
        hint = (
            f"\n\n---\n本技能还有附属文件：{'、'.join(attachments)}（用 skill_read_file 读取）"
            if attachments
            else ""
        )
        return f"# 技能：{skill.name}\n\n{body}{hint}"

    @tool
    def list_skill_files(name: str) -> str:
        """列出某个技能目录下的附属文件。

        Args:
            name: 技能名。

        Returns:
            相对路径清单；没有附属文件时说明情况。

        技能正文里提到 ``templates/xxx`` 这类文件时，用本工具确认确切路径，
        再用 skill_read_file 读取 —— 不要凭猜测拼路径。
        """
        skill = skills.get(name)
        if skill is None:
            available = "、".join(skills.names) or "（无）"
            raise SkillError(f"没有名为 {name!r} 的技能。可用：{available}")
        files = skills.list_files(name)
        if not files:
            return f"技能 {name!r} 没有附属文件。"
        return "、".join(files)

    @tool
    def skill_read_file(name: str, path: str) -> str:
        """读取技能目录下的附属文件（模板、参考资料等）。

        Args:
            name: 技能名。
            path: 相对技能目录的路径，例如 ``templates/report.md``。

        Returns:
            文件内容（超长会截断）。

        只在技能正文确实引用了该文件时才读；不要一次性把所有附属文件都读进来。
        """
        return skills.read_file(name, path)

    annotate(read_skill, group=GROUP, side_effects=False)
    annotate(list_skill_files, group=GROUP, side_effects=False)
    annotate(skill_read_file, group=GROUP, side_effects=False)
    return [read_skill, list_skill_files, skill_read_file]


__all__ = ["GROUP", "MAX_BODY_CHARS", "build_tools"]
