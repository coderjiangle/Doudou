"""Long-term memory write-back tool.

只提供一个工具：``remember``。**读**不需要工具 —— 记忆是在 Agent 构造时
就注入提示词的，模型本来就看得见，再给一个 ``recall`` 只会浪费一次调用。

写记忆是**有副作用**的操作（``side_effects=True``），因此默认需要人工确认。
在飞书/HTTP 这类没有确认通道的场景下会被拒绝 —— 这是刻意的：
没人能同意的时候，不该替用户往长期记忆里写东西。

没有配置记忆文件时不注册工具（延续「依赖不满足就不注册」）。
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.tools import BaseTool, tool

from doudou.agent.memory import MAX_FACT_CHARS, MemoryStore
from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.tools.decorators import annotate

logger = get_logger(__name__)

GROUP = "memory"


def build_tools(
    settings: Settings,
    *,
    store: MemoryStore | None = None,
) -> Sequence[BaseTool]:
    """构造记忆工具；没有配置记忆文件时返回空列表。"""
    memory = store if store is not None else MemoryStore(settings)
    if not memory.enabled:
        logger.debug("tools.memory_skipped", reason="agent.memory_files is empty")
        return []

    target = memory.target

    @tool
    def remember(fact: str) -> str:
        """把一条关于用户的长期事实记下来。

        Args:
            fact: 要记住的事实，一句话。例如「用户偏好中文回复」
                「用户在做 Doudou 这个项目」。

        Returns:
            写入结果。

        只记录**跨会话仍然成立**的偏好与事实（习惯、称谓、长期项目）。
        不要记录本次对话的临时信息、也不要记录你已经能从工具查到的东西 ——
        记忆每轮都会带进提示词，写多了会一直占着上下文。
        """
        try:
            written = memory.append(fact)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return (
            f"已记住：{written}\n（写入 {target}；本轮后续对话已生效）"
            if target
            else f"已记住：{written}"
        )

    annotate(remember, group=GROUP, side_effects=True)
    return [remember]


__all__ = ["GROUP", "MAX_FACT_CHARS", "build_tools"]
