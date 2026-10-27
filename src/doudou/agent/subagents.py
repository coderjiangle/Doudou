"""Subagent definitions loaded from config/agents.yaml.

子 Agent 的价值是**上下文隔离**：主 Agent 把一团乱麻（检索、比对、试错）丢给子 Agent，
子 Agent 在自己的上下文里折腾完，只回传一段结论。主 Agent 的窗口因此保持干净。

这里只负责「把 YAML 变成结构化定义」，不负责执行 —— 执行在
``tools/builtin/delegation.py``，因为那需要模型、工具注册表与确认回调。

两个约束：

* **子 Agent 不继承 ``task`` 工具** —— 否则一个子 Agent 又能派子 Agent，
  递归深度不可控；
* **工具白名单优先**：省略 ``tools`` 时继承，显式空列表时禁用，非空时只允许列出的工具。
  子 Agent 的职责越窄，
  它跑偏的空间就越小。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 子 Agent 不允许拿到的工具：给了它就能无限嵌套。
FORBIDDEN_TOOLS = frozenset({"task"})

#: 名字会成为 ``task`` 工具的参数取值，限制字符集。
_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class SubAgentError(ValueError):
    """子 Agent 定义不合法。"""


class SubAgentSpec(BaseModel):
    """一个子 Agent 的声明。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN)
    description: str
    #: 子 Agent 自己的系统提示词。**必须自洽** —— 它看不到主 Agent 的提示词。
    system_prompt: str = Field(default="", alias="prompt")
    #: 省略时继承主 Agent 工具（除 task）；显式 [] 表示不使用任何工具。
    tools: list[str] | None = None
    #: 子 Agent 专属技能目录。空表示不加载技能。
    skills: list[str] = Field(default_factory=list)
    #: 覆盖模型（``provider:model``）。省略或 null 表示继承主模型。
    model: str | None = None
    #: 子 Agent 的步数上限。它常做多步检索，所以比主 Agent 宽松些也无妨。
    max_steps: int = Field(default=6, ge=1)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @property
    def label(self) -> str:
        return f"subagent:{self.name}"


class SubAgentRegistry:
    """已加载的子 Agent 定义。"""

    def __init__(self, specs: Sequence[SubAgentSpec] = ()) -> None:
        self._specs = list(specs)
        self._by_name = {spec.name: spec for spec in self._specs}

    def __len__(self) -> int:
        return len(self._specs)

    @property
    def specs(self) -> list[SubAgentSpec]:
        return list(self._specs)

    @property
    def names(self) -> list[str]:
        return [spec.name for spec in self._specs]

    def get(self, name: str) -> SubAgentSpec | None:
        return self._by_name.get(name.strip())

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "tools": list(spec.tools) if spec.tools is not None else None,
                "skills": list(spec.skills),
                "model": spec.model,
                "max_steps": spec.max_steps,
            }
            for spec in self._specs
        ]


def load_subagents(path: Path | None) -> SubAgentRegistry:
    """读取 ``agents.yaml`` 里的 ``subagents`` 段。

    文件缺失或某条定义不合法时**只告警**，不阻止启动：子 Agent 是增强项，
    一个 YAML 拼写错误不该让整个服务起不来。但必须显式告警 ——
    否则用户会以为子 Agent 生效了（和 MCP 清单同一套取舍）。
    """
    if path is None or not path.is_file():
        logger.debug("subagents.file_missing", path=str(path))
        return SubAgentRegistry()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error("subagents.unreadable", path=str(path), error=str(exc))
        return SubAgentRegistry()
    if not isinstance(raw, dict):
        logger.error("subagents.invalid_root", path=str(path))
        return SubAgentRegistry()

    entries = raw.get("subagents") or []
    if not isinstance(entries, list):
        logger.error("subagents.invalid_section", path=str(path), hint="subagents 必须是列表")
        return SubAgentRegistry()

    specs: list[SubAgentSpec] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            logger.error("subagents.invalid_entry", entry=str(entry)[:80])
            continue
        try:
            spec = SubAgentSpec.model_validate(entry)
        except ValidationError as exc:
            logger.error(
                "subagents.invalid_spec",
                name=entry.get("name"),
                error="；".join(
                    f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
                    for item in exc.errors()
                ),
            )
            continue
        if spec.name in seen:
            logger.error("subagents.duplicate_name", name=spec.name)
            continue
        if not spec.system_prompt.strip():
            logger.error("subagents.missing_prompt", name=spec.name)
            continue
        seen.add(spec.name)
        specs.append(spec)

    logger.info("subagents.loaded", names=[spec.name for spec in specs])
    return SubAgentRegistry(specs)


def select_tools(spec: SubAgentSpec, available: Sequence[Any]) -> list[Any]:
    """按白名单挑出子 Agent 能用的工具。

    永远排除 ``task``（防无限嵌套）；省略白名单时继承，显式空列表时禁用工具。
    白名单里写了不存在的工具会告警 —— 静默忽略会让「我明明配了工具」变成谜。
    """
    candidate = [tool for tool in available if tool.name not in FORBIDDEN_TOOLS]
    if spec.tools is None:
        return candidate

    wanted = set(spec.tools)
    found = [tool for tool in candidate if tool.name in wanted]
    missing = sorted(wanted - {tool.name for tool in found})
    if missing:
        logger.warning(
            "subagents.unknown_tools",
            subagent=spec.name,
            missing=missing,
            available=sorted(tool.name for tool in candidate)[:20],
        )
    return found


@dataclass(slots=True)
class DelegationResult:
    """一次委派的结果。"""

    subagent: str
    text: str
    ok: bool = True
    error: str | None = None
    steps: int = 0
    tools: list[str] = field(default_factory=list)


__all__ = [
    "FORBIDDEN_TOOLS",
    "DelegationResult",
    "SubAgentError",
    "SubAgentRegistry",
    "SubAgentSpec",
    "load_subagents",
    "select_tools",
]
