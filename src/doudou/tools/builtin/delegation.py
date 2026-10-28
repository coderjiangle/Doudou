"""The ``task`` tool: delegate a subtask to a subagent.

主 Agent 用它把「一团乱麻」丢出去，只拿回一段结论 —— 这就是子 Agent 的全部价值：
**子 Agent 的检索、试错、中间结果都留在它自己的上下文里**，不占主 Agent 的窗口。

不做的事：

* **不共享历史** —— 子 Agent 只拿到 ``description``，看不到主对话。共享历史会让
  隔离失效（那就等于没省上下文）；
* **不回传过程** —— 只返回最终文本。过程留在日志里，供人排查；
* **不继承 ``task``** —— 见 ``agent/subagents.py`` 的 ``FORBIDDEN_TOOLS``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool, tool

from doudou.agent.models import ModelBuildError, build_model
from doudou.agent.prompts import render_skill_index
from doudou.agent.subagents import DelegationResult, SubAgentRegistry, SubAgentSpec, select_tools
from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.skills.registry import SkillRegistry
from doudou.tools.builtin import skills as skill_tools
from doudou.tools.decorators import annotate, annotations_of
from doudou.utils.tokens import count_messages_tokens

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查
    # 运行时不能 import：tools.registry → 本模块 → agent.react → tools.decorators
    # →（包 __init__）→ tools.registry 会成环。真正需要它是在调用时。
    from doudou.agent.react import ApprovalHook, ReactAgent

logger = get_logger(__name__)

GROUP = "delegation"

#: 子 Agent 返回内容的长度上限 —— 回传一大段就失去隔离的意义了。
MAX_REPORT_CHARS = 6000


def build_tools(
    settings: Settings,
    *,
    registry: SubAgentRegistry | None = None,
    available_tools: Sequence[BaseTool] = (),
    model: BaseChatModel | None = None,
    fallback_models: Sequence[BaseChatModel] = (),
    approval_hook: ApprovalHook | None = None,
    approval_policy: Callable[[Sequence[BaseTool]], dict[str, bool]] | None = None,
) -> Sequence[BaseTool]:
    """构造 ``task`` 工具；没有可用子 Agent 定义或主模型时返回空列表。

    Args:
        settings: 配置。
        registry: 子 Agent 定义；缺省从 ``settings.app.agent.subagents`` 读。
        available_tools: 主 Agent 手里的工具，子 Agent 从中按白名单挑。
        model: 主模型（子 Agent 未指定自己的模型时继承它）。
        approval_hook: 主 Agent 的确认回调。**必须往下传** ——
            否则子 Agent 调用有副作用的工具时无人可问，会被直接拒绝。
        approval_policy: 由工具清单算出 ``{工具名: 是否需确认}``。
            **必须往下传**：子 Agent 的工具集与主 Agent 不同，
            直接抄主 Agent 的策略会漏掉子 Agent 自己那批工具，
            结果就是「主 Agent 的写操作要确认，子 Agent 的不用」——静默绕过。

            用回调而不是直接 import：``tools.registry`` 已经 import 了本模块，
            反向 import 会成环。
    """
    agents = registry if registry is not None else _load(settings)
    if not len(agents):
        logger.debug("tools.task_skipped", reason="没有配置子 Agent")
        return []
    if model is None:
        logger.warning(
            "tools.task_skipped",
            reason="缺少模型或可用工具",
            has_model=model is not None,
            tools=len(available_tools),
        )
        return []

    # 独立模型在装配时校验、复用客户端；依赖不满足的子 Agent 不暴露给模型。
    child_models: dict[str, BaseChatModel] = {}
    runnable_specs: list[SubAgentSpec] = []
    for spec in agents.specs:
        try:
            child_models[spec.name] = (
                build_model(settings.app.model, secrets=settings.secrets, spec=spec.model)
                if spec.model is not None
                else model
            )
        except ModelBuildError as exc:
            logger.warning(
                "delegation.model_unavailable",
                subagent=spec.name,
                model=spec.model,
                error_type=type(exc).__name__,
            )
            continue
        runnable_specs.append(spec)
    agents = SubAgentRegistry(runnable_specs)
    if not len(agents):
        return []

    @tool
    async def task(description: str, subagent_type: str = "") -> str:
        """把一个子任务委派给专门的子 Agent，只取回它的一段结论。

        Args:
            description: 任务描述。**要说清目标与交付物** ——
                子 Agent 看不到我们的对话，只有这段文字。
            subagent_type: 用哪个子 Agent；留空表示用默认的那个。

        Returns:
            子 Agent 的结论（不含它的中间过程）。

        适合：需要多步检索/比对/试错的独立子任务。
        不适合：一句话就能回答的问题、以及需要延续当前对话上下文的任务 ——
        子 Agent 看不到我们的历史，来回澄清反而更慢。
        """
        spec = _pick(agents, subagent_type)
        if spec is None:
            return f"没有名为 {subagent_type!r} 的子 Agent。可用的有：{'、'.join(agents.names)}"

        # 技能只属于子 Agent：替换主 Agent 的技能工具，避免跨索引读取。
        # 扫描与文件读取下沉到线程，不阻塞其它会话的事件循环。
        child_tools, child_prompt = await asyncio.to_thread(
            _child_context, settings, spec, available_tools
        )
        from doudou.agent.react import ReactAgent

        # 子 Agent 的工具集与主 Agent 不同，所以确认策略必须**按子 Agent 自己的
        # 工具清单**重新算一遍。抄主 Agent 的会让子 Agent 的写操作静默免确认。
        child_interrupt_on = approval_policy(child_tools) if approval_policy else {}
        child = ReactAgent(
            model=child_models[spec.name],
            fallback_models=fallback_models,
            tools=child_tools,
            system_prompt=child_prompt,
            max_steps=spec.max_steps,
            # 按子 Agent 的工具清单算出来的确认策略；回调与主 Agent 共用。
            interrupt_on=child_interrupt_on,
            approval_hook=approval_hook,
        )

        logger.info(
            "delegation.started",
            subagent=spec.name,
            tools=[item.name for item in child_tools],
            model=spec.model or "inherited",
            task_chars=len(description),
        )
        result = await _run_child(child, spec_name=spec.name, description=description)

        if not result.ok:
            return f"子 Agent {spec.name} 未能完成任务：{result.error or '未知错误'}"

        logger.info(
            "delegation.finished",
            subagent=spec.name,
            steps=result.steps,
            report_chars=len(result.text),
            tools=result.tools,
        )
        header = f"来自子 Agent {spec.name} 的结论"
        if result.tools:
            header += f"（它用了：{'、'.join(dict.fromkeys(result.tools))}）"
        body = result.text.strip() or "（子 Agent 没有给出内容）"
        if len(body) > MAX_REPORT_CHARS:
            body = f"{body[:MAX_REPORT_CHARS]}\n\n（结论过长已截断）"
        return f"{header}\n\n{body}"

    # 把实际可用的子 Agent 清单补进工具描述，让主模型知道 subagent_type 有哪些可选项。
    # 只能事后追加：docstring 里做字符串插值会让它不再是合法的 __doc__。
    task.description = f"{task.description}\n\n{_render_subagent_options(agents)}"

    annotate(task, group=GROUP, side_effects=False)
    return [task]


def _render_subagent_options(agents: SubAgentRegistry) -> str:
    """生成 ``task`` 工具描述里的可用子 Agent 列表。"""
    default_name = agents.specs[0].name
    lines = [f"可用的 subagent_type（留空默认使用 `{default_name}`）："]
    for spec in agents.specs:
        description = " ".join(spec.description.split())
        lines.append(f"- `{spec.name}`：{description}")
    return "\n".join(lines)


def _child_context(
    settings: Settings, spec: SubAgentSpec, available: Sequence[BaseTool]
) -> tuple[list[BaseTool], str]:
    """按 config_dir 解析技能目录，并继续遵守工具白名单与全局禁用项。"""
    base = settings.config_dir
    roots = [
        path if path.is_absolute() else base / path
        for directory in spec.skills
        for path in [Path(directory).expanduser()]
    ]
    registry = SkillRegistry(roots)
    enabled = settings.app.tools.enabled_groups
    local_tools = (
        skill_tools.build_tools(settings, registry=registry)
        if not enabled or skill_tools.GROUP in enabled
        else ()
    )
    # 即便主 Agent 有技能，子 Agent 也只能读自己的目录；空 skills 不继承主索引。
    candidates = [item for item in available if annotations_of(item).group != skill_tools.GROUP]
    candidates.extend(local_tools)
    selected = select_tools(
        spec, [item for item in candidates if item.name not in settings.app.tools.disabled]
    )
    index = ""
    if any(item.name == "read_skill" for item in selected):
        index = render_skill_index(registry.skills, limit=settings.app.agent.skill_index_limit)
    elif registry.skills:
        logger.warning(
            "delegation.skills_unreachable",
            subagent=spec.name,
            hint="请在工具白名单中允许 read_skill，并启用 skills 分组",
        )
    return selected, "\n\n".join(part for part in (spec.system_prompt, index) if part)


def _load(settings: Settings) -> SubAgentRegistry:
    from doudou.agent.subagents import subagents_from_config

    return subagents_from_config(settings.app.agent)


def _pick(registry: SubAgentRegistry, requested: str) -> SubAgentSpec | None:
    """选子 Agent：指定了就用指定的，没指定就用第一个。

    没指定时用第一个而不是报错：模型经常省略 ``subagent_type``，
    为此拒掉一次委派没有意义。
    """
    if requested.strip():
        return registry.get(requested)
    return registry.specs[0] if registry.specs else None


async def _run_child(child: ReactAgent, *, spec_name: str, description: str) -> DelegationResult:
    """跑子 Agent，并把它的输入/输出记进日志（过程留在日志里，不进主上下文）。"""
    from langchain_core.messages import HumanMessage

    messages = [HumanMessage(content=description)]
    tokens_in = count_messages_tokens(messages)
    try:
        result = await child.ainvoke(messages)
    except Exception as exc:
        logger.error("delegation.failed", subagent=spec_name, error=str(exc), exc_info=True)
        return DelegationResult(
            subagent=spec_name, text="", ok=False, error=f"{type(exc).__name__}: {exc}"
        )

    logger.debug(
        "delegation.report",
        subagent=spec_name,
        input_tokens=tokens_in,
        output_tokens=count_messages_tokens([*result.messages[1:]]),
    )
    return DelegationResult(
        subagent=spec_name,
        text=result.text,
        ok=result.ok,
        error=result.error,
        steps=result.steps,
        tools=[event.tool_name or "" for event in result.events if event.type == "tool_call"],
    )


__all__ = ["GROUP", "MAX_REPORT_CHARS", "build_tools"]
