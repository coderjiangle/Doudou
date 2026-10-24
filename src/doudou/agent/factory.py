"""Build the ReAct agent and expose it as a reusable bundle."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from doudou.agent.context import ContextManager, HistorySummarizer, ResultOffloader
from doudou.agent.memory import MemoryStore
from doudou.agent.models import build_model
from doudou.agent.prompts import load_system_prompt
from doudou.agent.react import ApprovalHook, ReactAgent
from doudou.config.loader import Settings, get_settings
from doudou.observability.logging import get_logger
from doudou.skills.registry import SkillRegistry
from doudou.tools.registry import build_tools, effective_interrupt_on

logger = get_logger(__name__)

#: 保留的入口签名提示：M9 会用 ``create_deep_agent`` 作为可选引擎，
#: 届时 :class:`AgentBundle` 的字段保持不变，只替换 ``agent`` 的构造方式。


@dataclass(slots=True)
class AgentBundle:
    """一次组装完成、可直接运行的 Agent。"""

    settings: Settings
    model: BaseChatModel
    tools: list[BaseTool]
    system_prompt: str
    agent: ReactAgent
    skills: SkillRegistry | None = None
    memory: MemoryStore | None = None
    context: ContextManager | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.tools]

    def refresh_skills(self) -> int:
        """重新发现 Skill，并**就地**更新系统提示词。

        为什么这里能做真正的热重载（而 MCP 的 ``/mcp/refresh`` 做不到）：
        技能索引只是提示词里的一段文本，而 ``read_skill`` 在调用时才查索引，
        所以换掉提示词 + 刷新索引就够了，不需要重建 Agent。
        MCP 的工具列表是 Agent 构造时固定的，那个必须重启。

        Returns:
            刷新后的技能数量。
        """
        if self.skills is None:
            return 0
        count = self.skills.reload()
        self._apply_prompt()
        logger.info("agent.skills_refreshed", count=count)
        return count

    def refresh_memory(self) -> int:
        """重新读取记忆文件并更新系统提示词（``remember`` 工具写回后调用）。

        Returns:
            记忆正文字符数。
        """
        if self.memory is None:
            return 0
        self._apply_prompt()
        content = self.memory.load()
        logger.info("agent.memory_refreshed", chars=content.chars, files=len(content.files))
        return content.chars

    def _apply_prompt(self) -> None:
        """按当前的技能索引与记忆重新组装提示词，并交给 Agent。

        单点收敛：技能与记忆的更新都走它，避免两处各拼一遍导致顺序不一致。
        """
        skills = self.skills or SkillRegistry([])
        memory_text = self.memory.render() if self.memory is not None else ""
        self.system_prompt = load_system_prompt(
            self.settings, skills=skills.skills, memory=memory_text
        )
        self.agent.system_prompt = self.system_prompt


def build_agent(
    settings: Settings | None = None,
    *,
    model: BaseChatModel | None = None,
    tools: list[BaseTool] | None = None,
    extra_tools: Sequence[BaseTool] = (),
    model_spec: str | None = None,
    approval_hook: ApprovalHook | None = None,
) -> AgentBundle:
    """组装 Agent。

    Args:
        settings: 配置；缺省使用进程级缓存实例。
        model: 直接注入模型（测试用），优先于 ``model_spec`` 与配置。
        tools: 直接注入工具列表（测试用），优先于内置工具装配。
        extra_tools: 追加给 Agent 的工具，例如 MCP 拉取到的那些。
            它们与内置工具一起走注册表，因此同样会做重名检测与确认策略计算。
        model_spec: 覆盖 ``model.primary``，对应 CLI 的 ``--model``。
        approval_hook: 人工确认回调，配合 ``agent.interrupt_on``。

    Note:
        这是**同步**函数：MCP 工具的拉取是异步的，由调用方（lifespan / CLI）
        先 ``await`` 拉好再传进来。这样工厂保持无 IO，测试更好写。
    """
    resolved = settings or get_settings()

    if model is None:
        model = build_model(resolved.app.model, secrets=resolved.secrets, spec=model_spec)

    # 记忆每轮都带进提示词，因此要能热更新（remember 工具写回后立刻生效）。
    memory = MemoryStore(resolved)

    # Skill 索引只建一次，提示词与 read_skill 工具共用它 ——
    # 否则「提示词里列出的技能」与「能读到的技能」可能对不上。
    skills_registry = SkillRegistry.from_settings(resolved)
    if tools is None:
        tools = build_tools(
            resolved,
            extra=extra_tools,
            extra_source="mcp",
            skills_registry=skills_registry,
            memory_store=memory,
            model=model,
            approval_hook=approval_hook,
        )

    system_prompt = _compose_prompt(resolved, skills_registry, memory)

    # 大结果卸载：落在工作区内，模型能用 workspace_read 读回来。
    offloader = ResultOffloader(resolved)
    # 摘要压缩：用同一个模型来压，避免再引入一个「压缩专用模型」的配置面。
    context = ContextManager(resolved, summarizer=HistorySummarizer(model), offloader=offloader)

    # 副作用工具在配置未显式说明时自动需要确认，见 tools.registry.requires_approval
    interrupt_on = effective_interrupt_on(resolved, tools)
    agent = ReactAgent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        max_steps=resolved.app.agent.max_steps,
        max_tool_calls_per_step=resolved.app.agent.max_tool_calls_per_step,
        interrupt_on=interrupt_on,
        approval_hook=approval_hook,
        offloader=offloader,
    )

    metadata = {
        "agent_name": resolved.app.agent.name,
        "model": model_spec or resolved.app.model.primary,
        "tools": [tool.name for tool in tools],
        "requires_approval": sorted(name for name, needed in interrupt_on.items() if needed),
        "skills": skills_registry.names,
        "memory_files": [str(path) for path in memory.paths],
        "system_prompt_chars": len(system_prompt),
    }
    logger.info("agent.built", **metadata)

    bundle = AgentBundle(
        settings=resolved,
        model=model,
        tools=list(tools),
        system_prompt=system_prompt,
        agent=agent,
        skills=skills_registry,
        memory=memory,
        context=context,
        metadata=metadata,
    )
    # remember 工具写回记忆后，让 Agent 立刻用上新提示词 ——
    # 否则用户会看到「它说记住了，但下一轮还是不知道」。
    memory.on_change(bundle.refresh_memory)
    return bundle


def _compose_prompt(settings: Settings, skills: SkillRegistry, memory: MemoryStore) -> str:
    """统一组装提示词，供首次构造与各次热更新复用。"""
    return load_system_prompt(settings, skills=skills.skills, memory=memory.render())


__all__ = ["AgentBundle", "build_agent"]
