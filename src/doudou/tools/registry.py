"""Collect builtin tools, MCP tools and plugin tools into one list."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from doudou.agent.memory import MemoryStore
from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.scheduler.store import SchedulerStore
from doudou.skills.registry import SkillRegistry
from doudou.tools.builtin import (
    browser,
    clock,
    delegation,
    files,
    http,
    memory,
    schedule,
    self_config,
    shell,
    skills,
    web_search,
)
from doudou.tools.decorators import annotations_of, is_annotated

logger = get_logger(__name__)

#: 一个工具提供方：按配置构造一组工具。
ToolProvider = Callable[[Settings], Sequence[BaseTool]]

#: 内置工具提供方：``(分组名, 提供方)``。
#: 顺序决定工具在清单里的排列，也决定重名时谁被保留（先者优先）。
BUILTIN_PROVIDERS: tuple[tuple[str, ToolProvider], ...] = (
    ("web", web_search.build_tools),
    ("http", http.build_tools),
    ("browser", browser.build_tools),
    ("files", files.build_tools),
    ("shell", shell.build_tools),
    ("skills", skills.build_tools),
    ("memory", memory.build_tools),
    ("clock", clock.build_tools),
    #: 定时任务需要调度存储，同记忆/技能那样单独构造。
    ("schedule", schedule.build_tools),
    #: 委派放最后：它需要「其它工具」的清单，见 build_tools。
    ("delegation", delegation.build_tools),
    #: 配置自检也要工具清单，同样在循环之后单独构造。
    ("meta", self_config.build_tools),
)

#: 全部内置分组，供 ``config check`` 与文档引用。
ALL_GROUPS: tuple[str, ...] = tuple(group for group, _ in BUILTIN_PROVIDERS)

#: ``extra`` 工具的默认分组与来源。
EXTRA_GROUP = "mcp"
EXTRA_SOURCE = "mcp"

#: 这几个分组的提供方需要「Agent 上下文」里的对象（索引/记忆/工具清单/调度存储），
#: 因此不能走统一的 ``(settings) -> tools`` 形状，在 build_tools 里单独处理。
SKILLS_GROUP = "skills"
MEMORY_GROUP = "memory"
SCHEDULE_GROUP = "schedule"
DELEGATION_GROUP = "delegation"
META_GROUP = "meta"
CONTEXT_GROUPS = frozenset(
    {SKILLS_GROUP, MEMORY_GROUP, SCHEDULE_GROUP, DELEGATION_GROUP, META_GROUP}
)


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """工具的展示信息，用于 ``doudou tools list`` 与 ``GET /tools``。"""

    name: str
    description: str
    group: str
    source: str
    args: dict[str, str]
    requires_approval: bool = False
    side_effects: bool = False
    timeout: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "group": self.group,
            "source": self.source,
            "args": self.args,
            "requires_approval": self.requires_approval,
            "side_effects": self.side_effects,
            "timeout": self.timeout,
        }


def build_tools(
    settings: Settings,
    *,
    extra: Iterable[BaseTool] = (),
    extra_source: str = EXTRA_SOURCE,
    skills_registry: SkillRegistry | None = None,
    memory_store: MemoryStore | None = None,
    scheduler_store: SchedulerStore | None = None,
    model: BaseChatModel | None = None,
    fallback_models: Sequence[BaseChatModel] = (),
    approval_hook: Any | None = None,
) -> list[BaseTool]:
    """汇总所有工具，做分组开关、禁用名单与重名检测。

    Args:
        settings: 用于构造依赖配置的工具。
        extra: 额外工具，例如 MCP 拉取到的工具。
        extra_source: ``extra`` 的来源标记，仅用于日志。
        skills_registry: 复用已有的 Skill 索引。**必须与系统提示词用同一份**，
            否则提示词里列出的技能和 ``read_skill`` 能读到的会对不上。
        memory_store: 复用已有的记忆存储，同样要与提示词共用一份。
        scheduler_store: 定时任务存储；为 ``None`` 时不注册定时任务工具。
        model: 主模型；``task`` 工具需要它来驱动子 Agent。
        approval_hook: 主 Agent 的确认回调，**必须传给 ``task``** ——
            否则子 Agent 调用有副作用的工具时无人可问。

    Returns:
        工具列表，顺序为「内置 → extra → 委派」。被配置禁用的、
        以及依赖不满足的都不出现。
    """
    collected: list[BaseTool] = []
    seen: dict[str, str] = {}

    for group, provider in BUILTIN_PROVIDERS:
        if group in CONTEXT_GROUPS:
            continue  # 需要 Agent 上下文，见下面的单独处理
        if not group_enabled(settings, group):
            logger.debug("tools.group_disabled", group=group)
            continue
        for item in provider(settings):
            _add(collected, seen, item, source=f"builtin:{group}", settings=settings)

    for item in extra:
        _add(collected, seen, item, source=extra_source, settings=settings)

    if group_enabled(settings, SKILLS_GROUP):
        for item in skills.build_tools(settings, registry=skills_registry):
            _add(collected, seen, item, source=f"builtin:{SKILLS_GROUP}", settings=settings)

    if group_enabled(settings, MEMORY_GROUP):
        for item in memory.build_tools(settings, store=memory_store):
            _add(collected, seen, item, source=f"builtin:{MEMORY_GROUP}", settings=settings)

    if group_enabled(settings, SCHEDULE_GROUP):
        for item in schedule.build_tools(
            settings,
            store=scheduler_store,
            default_timezone=settings.app.runtime.timezone,
        ):
            _add(collected, seen, item, source=f"builtin:{SCHEDULE_GROUP}", settings=settings)

    # 委派必须放最后：它要把「上面这些工具」按白名单交给子 Agent。
    if group_enabled(settings, DELEGATION_GROUP) and model is not None:
        for item in delegation.build_tools(
            settings,
            available_tools=collected,
            model=model,
            fallback_models=fallback_models,
            approval_hook=approval_hook,
            # 让子 Agent 按**自己的**工具清单重新算确认策略
            approval_policy=lambda tools: effective_interrupt_on(settings, tools),
        ):
            _add(collected, seen, item, source=f"builtin:{DELEGATION_GROUP}", settings=settings)

    # 配置自检放最后：它要展示「实际装载」的工具清单（含 task）与分组状态。
    if group_enabled(settings, META_GROUP):
        for item in self_config.build_tools(
            settings,
            tools=collected,
            groups=describe_groups(settings),
        ):
            _add(collected, seen, item, source=f"builtin:{META_GROUP}", settings=settings)

    return collected


def group_enabled(settings: Settings, group: str) -> bool:
    """分组是否启用：``enabled_groups`` 为空表示全部启用。"""
    enabled = settings.app.tools.enabled_groups
    return not enabled or group in enabled


def _add(
    collected: list[BaseTool],
    seen: dict[str, str],
    item: BaseTool,
    *,
    source: str,
    settings: Settings,
) -> None:
    if item.name in settings.app.tools.disabled:
        logger.info("tools.disabled_by_config", tool=item.name)
        return
    if not is_annotated(item):
        # 未注解的工具会被当成「有副作用」而要求人工确认。这是安全的默认，
        # 但通常意味着提供方忘了调 annotate()，因此在构建时就喊出来。
        logger.warning("tools.missing_annotations", tool=item.name, source=source)
    if item.name in seen:
        logger.warning(
            "tools.duplicate_name",
            tool=item.name,
            kept=seen[item.name],
            dropped=source,
        )
        return
    seen[item.name] = source
    collected.append(item)


def tool_infos(
    settings: Settings,
    *,
    tools: Sequence[BaseTool] | None = None,
    extra: Iterable[BaseTool] = (),
    extra_group: str = EXTRA_GROUP,
    extra_source: str = EXTRA_SOURCE,
) -> list[ToolInfo]:
    """生成工具清单（含是否需要人工确认）。

    两种模式：

    * **不给 ``tools``** —— 遍历 provider 从配置推导，分组与来源在产生处确定。
      用于 ``config check``：那里想看的是「按当前配置**应该**装载什么」，
      不应该去连 MCP。
    * **给了 ``tools``** —— 描述**实际装载**的那批工具（``AgentBundle.tools``）。
      MCP 工具是运行时拉取的、不在 provider 里，只有这条路径能把它们列出来；
      分组与来源从工具自身的注解里读（``mcp:demo`` 之类）。
    """
    if tools is not None:
        return [
            _info_from_annotations(settings, item)
            for item in tools
            if item.name not in settings.app.tools.disabled
        ]

    infos: list[ToolInfo] = []
    seen: set[str] = set()

    for group, provider in BUILTIN_PROVIDERS:
        if not group_enabled(settings, group):
            continue
        # skills / memory 的提供方在此会各自去加载索引与记忆文件，构造得出来；
        # delegation（``task``）不行 —— 它需要**主模型**与「其它工具的清单」，
        # 那是运行期才有的东西。因此配置派生视图里没有 ``task``，
        # 要看实际装载请用 ``doudou tools list``（走 ``tools=`` 那条路径）。
        if group == DELEGATION_GROUP:
            continue
        items: Sequence[BaseTool] = (
            skills.build_tools(settings)
            if group == SKILLS_GROUP
            else memory.build_tools(settings)
            if group == MEMORY_GROUP
            else provider(settings)
        )
        for item in items:
            _collect_info(
                infos, seen, item, group=group, source=f"builtin:{group}", settings=settings
            )

    for item in extra:
        _collect_info(infos, seen, item, group=extra_group, source=extra_source, settings=settings)

    return infos


def _info_from_annotations(settings: Settings, item: BaseTool) -> ToolInfo:
    """从工具自带的注解推导展示信息。

    注解里的 ``group`` 已经带了来源语义（内置是 ``clock``，MCP 是 ``mcp:demo``），
    因此用「有没有冒号」来区分来源 —— 比再引入一个字段更省，也够明确。
    """
    annotations = annotations_of(item)
    group = annotations.group or "other"
    source = group if ":" in group else f"builtin:{group}"
    return ToolInfo(
        name=item.name,
        description=(item.description or "").strip(),
        group=group,
        source=source,
        args=describe_args(item),
        requires_approval=requires_approval(settings, item),
        side_effects=annotations.side_effects,
        timeout=annotations.timeout,
    )


def _collect_info(
    infos: list[ToolInfo],
    seen: set[str],
    item: BaseTool,
    *,
    group: str,
    source: str,
    settings: Settings,
) -> None:
    if item.name in seen or item.name in settings.app.tools.disabled:
        return
    seen.add(item.name)
    infos.append(
        ToolInfo(
            name=item.name,
            description=(item.description or "").strip(),
            group=group,
            source=source,
            args=describe_args(item),
            requires_approval=requires_approval(settings, item),
            side_effects=annotations_of(item).side_effects,
            timeout=annotations_of(item).timeout,
        )
    )


@dataclass(frozen=True, slots=True)
class GroupStatus:
    """一个内置分组为什么在/不在工具列表里。"""

    name: str
    enabled: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"group": self.name, "enabled": self.enabled, "reason": self.reason}


def describe_groups(settings: Settings) -> list[GroupStatus]:
    """逐分组说明启用状态与原因。

    「工具为什么不见了」是配置门控最常见的困惑，因此把判断依据直接暴露出来 ——
    ``doudou tools list`` 与 ``doudou config check`` 都用它。
    """
    declared = set(settings.app.tools.enabled_groups)
    statuses: list[GroupStatus] = []
    for group, _ in BUILTIN_PROVIDERS:
        if declared and group not in declared:
            statuses.append(GroupStatus(group, False, "未列入 tools.enabled_groups"))
            continue
        if group == "web":
            missing = web_search.missing_requirement(settings)
            if missing:
                statuses.append(GroupStatus(group, False, missing))
                continue
        if group == "shell":
            reason = shell.missing_requirement(settings)
            if reason:
                statuses.append(GroupStatus(group, False, reason))
                continue
        if group == "browser":
            reason = browser.missing_requirement(settings)
            if reason:
                statuses.append(GroupStatus(group, False, reason))
                continue
        if group == "http" and not settings.app.tools.http.allowed_domains:
            statuses.append(
                GroupStatus(group, False, "tools.http.allowed_domains 为空（默认拒绝一切出站请求）")
            )
            continue
        statuses.append(GroupStatus(group, True, "ok"))
    return statuses


def requires_approval(settings: Settings, tool: BaseTool) -> bool:
    """该工具是否需要人工确认。

    优先级：``agent.interrupt_on`` 的显式设置 > 副作用声明。
    也就是说 ``interrupt_on: {workspace_write: false}`` 能覆盖掉「有副作用就拦截」的默认。
    """
    explicit = settings.app.agent.interrupt_on.get(tool.name)
    if explicit is not None:
        return bool(explicit)
    if settings.app.agent.interrupt_on.get("*"):
        return True
    if settings.app.agent.require_approval_for_side_effects:
        return annotations_of(tool).side_effects
    return False


def effective_interrupt_on(settings: Settings, tools: Sequence[BaseTool]) -> dict[str, bool]:
    """展开成 ReAct 循环用的 ``{工具名: 是否确认}``。

    副作用自动拦截在这里落成显式规则 —— 让「为什么这个工具要确认」
    在日志与配置里都是可见的，而不是藏在某个隐式判断里。
    """
    policy: dict[str, bool] = dict(settings.app.agent.interrupt_on)
    for item in tools:
        policy[item.name] = requires_approval(settings, item)
    return policy


def describe_args(item: BaseTool) -> dict[str, str]:
    """提取 ``{参数名: 类型}``，用于清单展示。"""
    schema = getattr(item, "args_schema", None)
    fields = getattr(schema, "model_fields", None) or {}
    described: dict[str, str] = {}
    for name, field in fields.items():
        annotation = getattr(field, "annotation", None)
        described[name] = getattr(annotation, "__name__", str(annotation))
    return described


def find_tool(tools: Sequence[BaseTool], name: str) -> BaseTool | None:
    """按名字查工具。"""
    for item in tools:
        if item.name == name:
            return item
    return None


__all__ = [
    "ALL_GROUPS",
    "BUILTIN_PROVIDERS",
    "CONTEXT_GROUPS",
    "DELEGATION_GROUP",
    "EXTRA_GROUP",
    "EXTRA_SOURCE",
    "MEMORY_GROUP",
    "SKILLS_GROUP",
    "GroupStatus",
    "ToolInfo",
    "ToolProvider",
    "build_tools",
    "describe_args",
    "describe_groups",
    "effective_interrupt_on",
    "find_tool",
    "group_enabled",
    "requires_approval",
    "tool_infos",
]
