"""List, read and reload skills.

三个接口的分工：

* ``GET /skills``      —— 列出索引（name + description + 附属文件），不返回正文；
* ``GET /skills/{name}`` —— 读某个技能，**默认不含正文**（``include_body=true`` 才带），
  因为正文可能很大，检视索引时并不需要；
* ``POST /skills/reload`` —— 重新扫描并**就地更新系统提示词**。

重载能做到真正生效（不同于 MCP 刷新）是因为：技能索引只是提示词里的一段文本，
而 ``read_skill`` 在调用时才查索引，所以换掉提示词就够了。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from doudou.api.deps import StateDep
from doudou.api.errors import ApiError
from doudou.api.schemas import SkillOut, SkillReloadResponse, SkillsResponse
from doudou.observability.logging import get_logger
from doudou.skills.loader import SkillError

logger = get_logger(__name__)

router = APIRouter(tags=["skills"])


def _registry(state: StateDep):  # type: ignore[no-untyped-def]
    registry = state.bundle.skills
    if registry is None:
        raise ApiError(503, "skills_unavailable", "Skill 索引未初始化")
    return registry


@router.get("/skills", response_model=SkillsResponse, summary=" Skill 清单")
async def list_skills(state: StateDep) -> SkillsResponse:
    """列出已发现的技能索引（不含正文）。"""
    registry = _registry(state)
    return SkillsResponse(
        count=len(registry),
        roots=[str(root) for root in registry.roots],
        errors=registry.errors,
        skills=[SkillOut.model_validate(entry.as_dict()) for entry in registry.describe()],
    )


@router.get("/skills/{name}", response_model=SkillOut, summary="Skill 详情")
async def get_skill(
    name: str,
    state: StateDep,
    include_body: Annotated[
        bool, Query(description="是否包含正文（可能很大，默认不含）。")
    ] = False,
) -> SkillOut:
    """读一个技能的 frontmatter 与（可选的）正文。"""
    registry = _registry(state)
    skill = registry.get(name)
    if skill is None:
        raise ApiError(
            404,
            "skill_not_found",
            f"没有名为 {name} 的技能。可用：{', '.join(registry.names) or '（无）'}",
        )
    try:
        files = registry.list_files(name)
    except SkillError:  # pragma: no cover - 刚查到过，理论上不会失败
        files = []
    return SkillOut(
        name=skill.name,
        description=skill.description,
        path=str(skill.path),
        directory=str(skill.directory),
        body_chars=len(skill.body),
        body=skill.body if include_body else None,
        files=files,
        extra=dict(skill.extra),
    )


@router.post("/skills/reload", response_model=SkillReloadResponse, summary="重新扫描 Skill")
async def reload_skills(state: StateDep) -> SkillReloadResponse:
    """重新扫描 Skill 目录，并更新系统提示词 —— **无需重启服务**。

    不同于 MCP 的刷新：技能索引只是提示词里的一段文本，``read_skill`` 又在调用时
    才查索引，所以换掉提示词就真的生效了。
    """
    registry = _registry(state)
    count = state.bundle.refresh_skills()
    logger.info("api.skills_reloaded", count=count, requested=len(registry))
    return SkillReloadResponse(
        count=count,
        errors=registry.errors,
        system_prompt_chars=len(state.bundle.system_prompt),
        note="索引与系统提示词已更新，下次对话即生效。",
    )


__all__ = ["router"]
