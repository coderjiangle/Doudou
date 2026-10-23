"""Build the ReAct agent and expose it as a reusable bundle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from doudou.agent.models import build_model
from doudou.agent.prompts import load_system_prompt
from doudou.agent.react import ApprovalHook, ReactAgent
from doudou.config.loader import Settings, get_settings
from doudou.observability.logging import get_logger
from doudou.tools.registry import build_tools

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
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.tools]


def build_agent(
    settings: Settings | None = None,
    *,
    model: BaseChatModel | None = None,
    tools: list[BaseTool] | None = None,
    model_spec: str | None = None,
    approval_hook: ApprovalHook | None = None,
) -> AgentBundle:
    """组装 Agent。

    Args:
        settings: 配置；缺省使用进程级缓存实例。
        model: 直接注入模型（测试用），优先于 ``model_spec`` 与配置。
        tools: 直接注入工具列表（测试用），优先于内置工具装配。
        model_spec: 覆盖 ``model.primary``，对应 CLI 的 ``--model``。
        approval_hook: 人工确认回调，配合 ``agent.interrupt_on``。
    """
    resolved = settings or get_settings()

    if model is None:
        model = build_model(resolved.app.model, secrets=resolved.secrets, spec=model_spec)
    if tools is None:
        tools = build_tools(resolved)

    system_prompt = load_system_prompt(resolved)

    agent = ReactAgent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        max_steps=resolved.app.agent.max_steps,
        max_tool_calls_per_step=resolved.app.agent.max_tool_calls_per_step,
        interrupt_on=resolved.app.agent.interrupt_on,
        approval_hook=approval_hook,
    )

    metadata = {
        "agent_name": resolved.app.agent.name,
        "model": model_spec or resolved.app.model.primary,
        "tools": [tool.name for tool in tools],
        "system_prompt_chars": len(system_prompt),
    }
    logger.debug("agent.built", **metadata)

    return AgentBundle(
        settings=resolved,
        model=model,
        tools=list(tools),
        system_prompt=system_prompt,
        agent=agent,
        metadata=metadata,
    )


__all__ = ["AgentBundle", "build_agent"]
