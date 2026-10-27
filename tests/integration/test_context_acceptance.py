"""长会话不炸上下文、超大结果不顶坏后续、记忆在新会话生效。

这些用例跑的是**完整链路**（AgentRunner + 落库 + 上下文管理），而不是单个函数 ——
上下文的三个机制彼此的交互才是真正的风险所在。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from doudou.agent.context import ContextManager, ResultOffloader
from doudou.agent.factory import build_agent
from doudou.agent.testing import ScriptedChatModel, tool_call
from doudou.config.loader import Settings
from doudou.gateway.runner import AgentRunner
from doudou.gateway.schema import RunRequest


class FixedSummarizer:
    """确定性摘要器：内容固定，便于断言。"""

    def __init__(self, text: str = "此前用户问了若干轮问题，助手都给了简短答复。") -> None:
        self.text = text
        self.calls = 0

    async def __call__(self, messages) -> str:  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.text


@pytest.fixture
def tight_settings(settings_factory, tmp_path: Path):  # type: ignore[no-untyped-def]
    """把预算压小、保留条数拉高，让 50 轮就能触发压缩。"""

    def make(*, max_input_tokens: int = 1200, retention: int = 1000) -> Settings:
        return settings_factory(
            config_yaml=(
                "filesystem:\n"
                f'  root_dir: "{(tmp_path / "workspace").as_posix()}"\n'
                "context:\n"
                f"  max_input_tokens: {max_input_tokens}\n"
                "  summarize_threshold_ratio: 0.5\n"
                "  keep_recent_messages: 4\n"
                "storage:\n"
                f"  max_messages_per_thread: {retention}\n"
                "  history: memory\n"
            )
        )

    return make


def paged_model() -> ScriptedChatModel:
    """每轮都给一段有长度的答复，让 token 消耗真实增长。"""
    return ScriptedChatModel(
        steps=[],
        fallback_text="好的，我记下了。" + "补充说明。" * 20,
    )


def _plain_model() -> ScriptedChatModel:
    return ScriptedChatModel(steps=[], fallback_text="没问题")


def _tool_then_answer() -> ScriptedChatModel:
    """第一轮要求调用工具，之后直接收尾。"""
    return ScriptedChatModel(
        steps=[tool_call("fetch_report", {}, call_id="c1"), AIMessage(content="已阅")]
    )


def _with_offload_threshold(settings: Settings, threshold: int) -> Settings:
    """把卸载阈值调到很小的一个副本（测试用，避免真的写 3.6 万字）。"""
    context = settings.app.context.model_copy(update={"offload_threshold_bytes": threshold})
    app = settings.app.model_copy(update={"context": context})
    return settings.model_copy(update={"app": app})


def _rebuild_agent(
    bundle,  # type: ignore[no-untyped-def]
    *,
    tools: list,
    offloader: ResultOffloader,
    model: ScriptedChatModel,
) -> object:
    """按给定工具/卸载器重建一个 ReactAgent（保留原提示词与确认策略）。"""
    from doudou.agent.react import ReactAgent

    return ReactAgent(
        model=model,
        tools=tools,
        system_prompt=bundle.system_prompt,
        interrupt_on={tool.name: False for tool in tools},
        offloader=offloader,
    )


# --------------------------------------------------------------------------- 50 轮


async def test_fifty_rounds_stay_within_budget(tight_settings) -> None:  # type: ignore[no-untyped-def]
    """验收：连续 50 轮对话不炸上下文。"""
    settings = tight_settings()
    bundle = build_agent(settings, model=paged_model())
    summarizer = FixedSummarizer()
    bundle.context = ContextManager(settings, summarizer=summarizer)
    runner = AgentRunner(bundle)

    for index in range(50):
        result = await runner.ainvoke(
            RunRequest(text=f"第 {index} 轮提问，" + "内容" * 30, conversation_id="c1")
        )
        assert result.ok, f"第 {index} 轮失败了"

    thread_id = await runner.resolve_thread(RunRequest(text="", conversation_id="c1"))
    stats = runner.context_stats(thread_id)

    assert stats is not None
    # 送进模型的窗口始终在预算内 —— 这是「不炸」的定义
    assert stats.tokens <= stats.budget, stats.as_dict()
    # 而且确实触发过压缩（否则这个用例没测到东西）
    assert stats.compressed is True
    assert stats.summarized > 0


async def test_history_is_never_deleted_by_compression(tight_settings) -> None:  # type: ignore[no-untyped-def]
    """压缩只影响送给模型的窗口；库里必须留着完整历史，否则无法回溯。"""
    settings = tight_settings()
    bundle = build_agent(settings, model=paged_model())
    bundle.context = ContextManager(settings, summarizer=FixedSummarizer())
    runner = AgentRunner(bundle)

    for index in range(30):
        await runner.ainvoke(RunRequest(text=f"第 {index} 轮", conversation_id="c1"))

    thread_id = await runner.resolve_thread(RunRequest(text="", conversation_id="c1"))
    stored = await runner.history(thread_id)

    # 30 轮 × (提问 + 答复) = 60 条，一条都不能少
    assert len(stored) == 60
    assert stored[0].content.startswith("第 0 轮")


async def test_context_report_explains_state(tight_settings) -> None:  # type: ignore[no-untyped-def]
    """排查「它为什么忘了」时要能一眼看出：占多少、压过没、原文还在不在。"""
    settings = tight_settings()
    bundle = build_agent(settings, model=paged_model())
    bundle.context = ContextManager(settings, summarizer=FixedSummarizer("摘要内容"))
    runner = AgentRunner(bundle)

    for index in range(30):
        await runner.ainvoke(RunRequest(text=f"第 {index} 轮", conversation_id="c1"))

    thread_id = await runner.resolve_thread(RunRequest(text="", conversation_id="c1"))
    report = await runner.context_report(thread_id)

    assert report["thread_id"] == thread_id
    assert report["stored_messages"] == 60
    assert report["stored_tokens"] > report["budget"]  # 原始历史确实超出预算
    assert report["last_run"]["tokens"] <= report["budget"]  # 但送进去的在预算内
    assert report["last_run"]["compressed"] is True
    assert report["summary"]["present"] is True
    assert "原始历史全部保留在库里" in report["note"]


async def test_summarizer_called_but_not_every_turn(tight_settings) -> None:  # type: ignore[no-untyped-def]
    """摘要要复用：每轮重压既慢又贵，还会让摘要漂移。"""
    settings = tight_settings()
    bundle = build_agent(settings, model=paged_model())
    summarizer = FixedSummarizer()
    bundle.context = ContextManager(settings, summarizer=summarizer)
    runner = AgentRunner(bundle)

    for index in range(30):
        await runner.ainvoke(RunRequest(text=f"第 {index} 轮", conversation_id="c1"))

    # 30 轮里压缩若干次，但远少于 30 次
    assert 0 < summarizer.calls < 30


async def test_conversations_have_independent_budgets(tight_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tight_settings()
    bundle = build_agent(settings, model=paged_model())
    bundle.context = ContextManager(settings, summarizer=FixedSummarizer())
    runner = AgentRunner(bundle)

    for index in range(20):
        await runner.ainvoke(RunRequest(text=f"a{index}", conversation_id="conv-a"))
    await runner.ainvoke(RunRequest(text="b0", conversation_id="conv-b"))

    thread_a = await runner.resolve_thread(RunRequest(text="", conversation_id="conv-a"))
    thread_b = await runner.resolve_thread(RunRequest(text="", conversation_id="conv-b"))

    assert runner.context_stats(thread_a) is not None
    assert runner.context_stats(thread_b) is not None
    assert len(await runner.history(thread_a)) == 40
    assert len(await runner.history(thread_b)) == 2


async def test_clear_thread_forgets_summary_cache(tight_settings) -> None:  # type: ignore[no-untyped-def]
    settings = tight_settings()
    bundle = build_agent(settings, model=paged_model())
    manager = ContextManager(settings, summarizer=FixedSummarizer())
    bundle.context = manager
    runner = AgentRunner(bundle)

    for index in range(30):
        await runner.ainvoke(RunRequest(text=f"第 {index} 轮", conversation_id="c1"))
    thread_id = await runner.resolve_thread(RunRequest(text="", conversation_id="c1"))
    assert manager.summary_for(thread_id)

    await runner.clear_thread(thread_id)

    assert manager.summary_for(thread_id) is None
    assert runner.context_stats(thread_id) is None


# --------------------------------------------------------------------------- 超大工具结果


async def test_huge_tool_result_does_not_break_following_turns(tight_settings) -> None:  # type: ignore[no-untyped-def]
    """验收：一次返回 5 万字的结果不会把后续请求顶坏。"""
    settings = tight_settings()
    huge = "巨量内容" * 3000  # 约 3.6 万字，远超默认卸载阈值

    @tool
    def fetch_report() -> str:
        """返回一大段内容。"""
        return huge

    offloader = ResultOffloader(_with_offload_threshold(settings, 1000))
    bundle = build_agent(settings, model=paged_model())
    bundle.context = ContextManager(settings, summarizer=FixedSummarizer())
    # 直接换掉 Agent 的工具与模型：这个用例只关心「超大结果进上下文后长什么样」
    bundle.tools = [fetch_report]
    bundle.agent = _rebuild_agent(
        bundle, tools=[fetch_report], offloader=offloader, model=_tool_then_answer()
    )
    runner = AgentRunner(bundle)

    first = await runner.ainvoke(RunRequest(text="读那份报告", conversation_id="c1"))
    assert first.ok

    tool_messages = [m for m in first.messages if m.type == "tool"]
    assert tool_messages
    content = str(tool_messages[0].content)
    assert "已存为文件" in content
    assert len(content) < 3000
    assert huge not in content

    # 后续轮次必须照常可用
    bundle.agent.model = _plain_model()
    bundle.agent._runnable = bundle.agent.model
    second = await runner.ainvoke(RunRequest(text="接着说", conversation_id="c1"))
    assert second.ok
    assert offloader.count == 1


async def test_offloaded_file_is_readable_in_workspace(tight_settings) -> None:  # type: ignore[no-untyped-def]
    """卸载目录必须在工作区内 —— 否则模型读不回来，卸载等于丢弃。"""
    from doudou.tools.builtin.files import build_tools as build_file_tools

    settings = tight_settings()
    offloader = ResultOffloader(
        settings.model_copy(
            update={
                "app": settings.app.model_copy(
                    update={
                        "context": settings.app.context.model_copy(
                            update={"offload_threshold_bytes": 50}
                        )
                    }
                )
            }
        )
    )

    result = offloader.maybe_offload("大内容" * 200, name="web_search", call_id="call_abcd1234")
    relative = result.split("`")[1]

    read_tool = {item.name: item for item in build_file_tools(settings)}["workspace_read"]
    assert "大内容" in read_tool.invoke({"path": relative})


# --------------------------------------------------------------------------- 长期记忆


def test_memory_preference_applies_to_new_sessions(settings_factory, tmp_path: Path) -> None:
    """验收：AGENTS.md 里写的偏好在新会话中生效。"""
    memory_file = tmp_path / "AGENTS.md"
    memory_file.write_text("用户偏好中文回复，且不喜欢大段贴原文。", encoding="utf-8")
    settings = settings_factory(
        config_yaml=(f"agent:\n  memory_files:\n    - {json.dumps(str(memory_file))}\n")
    )

    bundle = build_agent(settings, model=paged_model())

    assert "用户偏好中文回复" in bundle.system_prompt
    # 记忆是每轮都带的（不等于 Skill 的按需加载）
    assert "## 长期记忆" in bundle.system_prompt


async def test_memory_survives_multiple_threads(settings_factory, tmp_path: Path) -> None:
    """记忆属于 Agent，不属于某个会话 —— 新会话同样带着它。"""
    memory_file = tmp_path / "AGENTS.md"
    memory_file.write_text("用户在做一个叫 Doudou 的个人助手。", encoding="utf-8")
    settings = settings_factory(
        config_yaml=(f"agent:\n  memory_files:\n    - {json.dumps(str(memory_file))}\n")
    )
    bundle = build_agent(settings, model=paged_model())
    runner = AgentRunner(bundle)

    await runner.ainvoke(RunRequest(text="你好", conversation_id="conv-a"))
    await runner.ainvoke(RunRequest(text="你好", conversation_id="conv-b", new_thread=True))

    thread_b = await runner.resolve_thread(RunRequest(text="", conversation_id="conv-b"))
    assert len(await runner.history(thread_b)) == 2
    # 两个会话的提示词都带着记忆
    assert "Doudou" in bundle.agent.system_prompt
