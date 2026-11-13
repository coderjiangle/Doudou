"""System prompt loading and composition."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.skills.loader import SkillInfo

logger = get_logger(__name__)

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: 未配置 ``agent.system_prompt_file`` 时使用的内置提示词。
#:
#: 这里只写「人设 + 行为边界」。工具清单由模型自己从 tools schema 里看到，
#: 不需要在提示词里逐条复述，否则既冗长又容易过期。
DEFAULT_SYSTEM_PROMPT = """\
你是 {{ name }}，{{ user_name }} 的个人助手。

## 风格
- 先给结论，再补必要的依据，不说客套话。
- 默认使用中文回复。
- 不确定就说不确定，绝不编造事实、数字、链接或工具没有返回的内容。

## 工作方式
- 需要外部或实时信息时，先调用工具再回答，不要凭记忆猜测。
- 一次只解决一个问题；复杂任务先拆解再逐步推进。
- 工具返回错误时，说明失败原因并给出可行的下一步，不要假装成功。

## 边界
- 涉及不可逆操作前，先说明将要做什么。
- 不泄露密钥、令牌、内部路径等敏感信息。
"""


def render_template(text: str, variables: dict[str, Any] | None = None) -> str:
    """替换 ``{{ key }}`` 占位符；未提供的 key 保留原样以便发现遗漏。"""
    values = dict(variables or {})

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        return str(values[key])

    return _PLACEHOLDER.sub(_substitute, text)


def default_variables(settings: Settings) -> dict[str, Any]:
    """提示词模板可用变量。"""
    return {
        "name": settings.app.agent.name,
        "user_name": os.environ.get("DOUDOU_USER_NAME", "你"),
        "timezone": settings.app.runtime.timezone,
        "env": settings.app.runtime.env,
    }


def load_system_prompt(
    settings: Settings,
    *,
    variables: dict[str, Any] | None = None,
    skills: Sequence[SkillInfo] = (),
    skill_index_limit: int | None = None,
    memory: str = "",
) -> str:
    """组装系统提示词：模板 + 长期记忆 + 技能索引。

    Args:
        settings: 已解析的配置。
        variables: 覆盖默认模板变量。
        skills: 已发现的 Skill，**只注入索引**（name + description）。
        skill_index_limit: 索引里最多列几个；缺省取 ``agent.skill_index_limit``。
        memory: 长期记忆正文。它**每轮都会带**，因此调用方需要控制长度。
    """
    path = settings.system_prompt_path
    if path and path.is_file():
        template = path.read_text(encoding="utf-8")
    else:
        if path:
            logger.warning("prompt.file_missing", path=str(path))
        template = DEFAULT_SYSTEM_PROMPT

    merged_vars = default_variables(settings)
    merged_vars.update(variables or {})

    limit = skill_index_limit or settings.app.agent.skill_index_limit
    index = render_skill_index(skills, limit=limit)
    memory_block = memory.strip()

    has_memory_slot = bool(re.search(r"\{\{\s*memory\s*\}\}", template))
    has_skills_slot = bool(re.search(r"\{\{\s*skills\s*\}\}", template))
    if has_memory_slot:
        merged_vars.setdefault("memory", memory_block)
    if has_skills_slot:
        merged_vars.setdefault("skills", index)

    prompt = render_template(template, merged_vars).strip()
    # 槽位为空时可能会留下连续空行，收敛成最多一个空行。
    prompt = re.sub(r"\n{3,}", "\n\n", prompt)

    if not has_memory_slot and memory_block:
        prompt = f"{prompt}\n\n{memory_block}"

    if not has_skills_slot and index:
        prompt = f"{prompt}\n\n{index}"

    return prompt


def render_skill_index(skills: Sequence[SkillInfo], *, limit: int = 40) -> str:
    """渲染「可用技能」一节。

    **只放 name 与 description**：正文不进上下文预算，模型需要时用
    ``read_skill`` 去读（渐进式披露）。

    超出 ``limit`` 时截断并说明还有多少 —— 索引必须是有界的，
    否则技能一多，光是索引就把上下文顶坏了。
    """
    if not skills:
        return ""

    listed = list(skills)[:limit]
    lines = [
        "## 可用技能",
        "",
        "以下技能提供特定领域的**做法**。系统提示词里只有它们的摘要，",
        '需要时用 `read_skill("<名字>")` 读取完整步骤 ——'
        "**与当前任务无关的技能不要读**，那只会浪费上下文。",
        "",
    ]
    for skill in listed:
        description = " ".join(skill.description.split())
        lines.append(f"- `{skill.name}`：{description}")
    if len(skills) > len(listed):
        lines.append("")
        lines.append(f"（另有 {len(skills) - len(listed)} 个技能未列出）")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "default_variables",
    "load_system_prompt",
    "render_skill_index",
    "render_template",
]
