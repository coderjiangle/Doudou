"""上下文工程：token 计数、预算裁剪、摘要压缩、大结果卸载、长期记忆。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from doudou.agent.context import (
    SUMMARY_PREFIX,
    ContextManager,
    ContextStats,
    HistorySummarizer,
    ResultOffloader,
    trim_to_budget,
)
from doudou.agent.memory import MEMORY_SECTION_TITLE, MemoryStore
from doudou.agent.testing import ScriptedChatModel
from doudou.config.loader import Settings
from doudou.utils.tokens import (
    count_message_tokens,
    count_messages_tokens,
    count_tokens,
    estimate_tokens,
    reset_encoder_cache,
)

# --------------------------------------------------------------------------- token 计数


def test_count_tokens_empty() -> None:
    assert count_tokens("") == 0


def test_count_tokens_positive() -> None:
    assert count_tokens("hello world") > 0
    assert count_tokens("你好世界") > 0


def test_count_tokens_grows_with_length() -> None:
    assert count_tokens("x" * 400) > count_tokens("x" * 40)


def test_estimate_tokens_handles_cjk() -> None:
    """CJK 每个字约 1 token；兜底估算不能把中文当成 4 字符/token。"""
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcd") == 1


def test_estimate_tokens_empty() -> None:
    assert estimate_tokens("") == 0


def test_count_message_tokens_includes_overhead() -> None:
    """空消息也有角色开销 —— 忽略它会让估算系统性偏低。"""
    assert count_message_tokens(AIMessage(content="")) >= 4


def test_count_message_tokens_includes_tool_calls() -> None:
    plain = count_message_tokens(AIMessage(content="x"))
    with_call = count_message_tokens(
        AIMessage(
            content="x",
            tool_calls=[{"name": "search", "args": {"q": "很长的一串参数"}, "id": "c1"}],
        )
    )

    assert with_call > plain


def test_count_message_tokens_includes_tool_metadata() -> None:
    bare = count_message_tokens(ToolMessage(content="ok", tool_call_id=""))
    full = count_message_tokens(
        ToolMessage(content="ok", tool_call_id="call_abcdefgh", name="web_search")
    )

    assert full > bare


def test_count_messages_tokens_sums() -> None:
    messages = [HumanMessage(content="a"), AIMessage(content="b")]

    assert count_messages_tokens(messages) == sum(
        count_message_tokens(message) for message in messages
    )


def test_count_tokens_falls_back_when_encoder_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """离线时 tiktoken 取不到词表，必须退回字符估算而不是崩。"""
    import doudou.utils.tokens as tokens

    reset_encoder_cache()
    monkeypatch.setattr(tokens, "get_encoding", lambda: None)

    assert tokens.count_tokens("你好世界") == 4


def test_reset_encoder_cache_is_safe() -> None:
    reset_encoder_cache()
    assert count_tokens("abc") > 0
    reset_encoder_cache()


# --------------------------------------------------------------------------- 预算裁剪


def make_history(count: int, *, content: str = "一段中等长度的内容，大约二十来个 token") -> list:
    """构造 ``count * 2`` 条消息的历史（一问一答）。"""
    messages: list = []
    for index in range(count):
        messages.append(HumanMessage(content=f"{content} {index}"))
        messages.append(AIMessage(content=f"答复 {index}：{content}"))
    return messages


def test_trim_to_budget_keeps_everything_when_small() -> None:
    messages = [HumanMessage(content="短")]

    assert trim_to_budget(messages, max_tokens=10_000) == messages


def test_trim_to_budget_drops_oldest() -> None:
    messages = make_history(50)

    kept = trim_to_budget(messages, max_tokens=200)

    assert 0 < len(kept) < len(messages)
    # 保留的必须是**最新**的那批
    assert kept[-1] is messages[-1]


def test_trim_to_budget_always_keeps_last_message() -> None:
    """返回空历史会让模型直接看不到用户刚说的话。"""
    messages = [HumanMessage(content="很长" * 5000)]

    kept = trim_to_budget(messages, max_tokens=1)

    assert len(kept) == 1
    assert kept[0] is messages[-1]


def test_trim_to_budget_never_starts_with_tool_result() -> None:
    """不切断工具调用组：起点落在工具结果上就往后顺延。"""
    messages = [
        HumanMessage(content="问题" * 100),
        AIMessage(
            content="调用工具",
            tool_calls=[{"name": "t", "args": {}, "id": "c1"}],
        ),
        ToolMessage(content="结果" * 100, tool_call_id="c1"),
        AIMessage(content="结论"),
    ]

    kept = trim_to_budget(messages, max_tokens=count_message_tokens(messages[2]))

    assert kept[0].type != "tool"


def test_trim_to_budget_empty_input() -> None:
    assert trim_to_budget([], max_tokens=100) == []


# --------------------------------------------------------------------------- 摘要压缩


class FakeSummarizer:
    """记录被压缩的内容，返回固定摘要。"""

    def __init__(self, text: str = "早期对话的摘要。") -> None:
        self.text = text
        self.calls: list[list] = []

    async def __call__(self, messages) -> str:  # type: ignore[no-untyped-def]
        self.calls.append(list(messages))
        return self.text


@pytest.fixture
def context_settings(settings_factory):  # type: ignore[no-untyped-def]
    def make(*, max_input_tokens: int = 2000, keep_recent: int = 4) -> Settings:
        return settings_factory(
            config_yaml=(
                "context:\n"
                f"  max_input_tokens: {max_input_tokens}\n"
                f"  keep_recent_messages: {keep_recent}\n"
                "  summarize_threshold_ratio: 0.5\n"
                "  offload_large_results: false\n"
            )
        )

    return make


async def test_no_compression_below_threshold(context_settings) -> None:  # type: ignore[no-untyped-def]
    summarizer = FakeSummarizer()
    manager = ContextManager(context_settings(), summarizer=summarizer)

    kept, stats = await manager.prepare("t1", [HumanMessage(content="短")])

    assert stats.compressed is False
    assert summarizer.calls == []
    assert len(kept) == 1


async def test_compression_replaces_old_messages(context_settings) -> None:  # type: ignore[no-untyped-def]
    summarizer = FakeSummarizer("用户想调研 Python 与 Go。")
    manager = ContextManager(context_settings(), summarizer=summarizer)
    messages = make_history(200)

    kept, stats = await manager.prepare("t1", messages)

    assert stats.compressed is True
    assert stats.summarized > 0
    assert len(kept) < len(messages)
    # 第一条必须是摘要
    assert isinstance(kept[0], SystemMessage)
    assert SUMMARY_PREFIX in str(kept[0].content)
    assert "Python 与 Go" in str(kept[0].content)
    # 最近的消息必须原样保留
    assert kept[-1] is messages[-1]


async def test_compression_keeps_recent_messages_intact(context_settings) -> None:  # type: ignore[no-untyped-def]
    manager = ContextManager(context_settings(keep_recent=6), summarizer=FakeSummarizer())
    messages = make_history(200)

    kept, _stats = await manager.prepare("t1", messages)

    tail = kept[-6:]
    assert tail == messages[-6:]


async def test_summary_is_reused_incrementally(context_settings) -> None:  # type: ignore[no-untyped-def]
    """逐轮复用摘要：从零重压既慢又贵，还会让摘要随轮次漂移。"""
    summarizer = FakeSummarizer("第一版摘要")
    manager = ContextManager(context_settings(), summarizer=summarizer)

    await manager.prepare("t1", make_history(200))
    first_calls = len(summarizer.calls)

    # 历史长了一点，但**没有超过缓存已覆盖的部分**
    await manager.prepare("t1", make_history(201))

    assert len(summarizer.calls) == first_calls
    assert manager.summary_for("t1") == "第一版摘要"


async def test_summary_extends_when_history_grows(context_settings) -> None:  # type: ignore[no-untyped-def]
    summarizer = FakeSummarizer()
    manager = ContextManager(context_settings(keep_recent=4), summarizer=summarizer)

    await manager.prepare("t1", make_history(150))
    first_calls = len(summarizer.calls)

    await manager.prepare("t1", make_history(400))

    assert len(summarizer.calls) > first_calls
    # 增量压缩：第二次的输入里应当带上上一版摘要
    assert any(
        SUMMARY_PREFIX in str(getattr(message, "content", "")) for message in summarizer.calls[-1]
    )


async def test_summarizer_failure_degrades(context_settings) -> None:  # type: ignore[no-untyped-def]
    """压缩失败不能中断对话：退回「只裁剪」，并如实记下来。"""

    class Broken:
        async def __call__(self, messages) -> str:  # type: ignore[no-untyped-def]
            raise RuntimeError("模型挂了")

    manager = ContextManager(context_settings(), summarizer=Broken())
    del manager  # 上面那行只是确认 Broken 的形状；真正的降级在 HistorySummarizer

    summarizer = HistorySummarizer(ScriptedChatModel(steps=[]))
    summarizer.model = _FailingModel()  # type: ignore[assignment]

    assert await summarizer([HumanMessage(content="内容" * 500)]) == ""


class _FailingModel:
    async def ainvoke(self, messages):  # type: ignore[no-untyped-def]
        raise RuntimeError("模型不可用")


async def test_compression_disabled(context_settings) -> None:  # type: ignore[no-untyped-def]
    settings = context_settings()
    disabled = settings.model_copy(
        update={
            "app": settings.app.model_copy(
                update={"context": settings.app.context.model_copy(update={"summarization": False})}
            )
        }
    )
    manager = ContextManager(disabled, summarizer=FakeSummarizer())

    _kept, stats = await manager.prepare("t1", make_history(200))

    assert stats.compressed is False
    assert manager.enabled is False


async def test_forget_clears_cache(context_settings) -> None:  # type: ignore[no-untyped-def]
    manager = ContextManager(context_settings(), summarizer=FakeSummarizer())
    await manager.prepare("t1", make_history(200))
    assert manager.summary_for("t1")

    manager.forget("t1")

    assert manager.summary_for("t1") is None
    assert manager.cached_threads() == []


async def test_split_point_does_not_break_tool_group(context_settings) -> None:  # type: ignore[no-untyped-def]
    """切点落在工具结果上会产出 provider 拒绝的历史。"""
    messages = [
        HumanMessage(content="问" * 200),
        AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]),
        ToolMessage(content="结果" * 200, tool_call_id="c1"),
        AIMessage(content="答"),
        HumanMessage(content="再问"),
        AIMessage(content="再答"),
    ]
    manager = ContextManager(context_settings(keep_recent=3), summarizer=FakeSummarizer())

    split = manager._split_point(messages, 3)

    assert messages[split].type != "tool"


def test_stats_as_dict() -> None:
    stats = ContextStats(messages=3, tokens=120, budget=1000)

    payload = stats.as_dict()

    assert payload["usage_ratio"] == 0.12
    assert payload["compressed"] is False


# --------------------------------------------------------------------------- 大结果卸载


@pytest.fixture
def offload_settings(settings_factory, tmp_path: Path):  # type: ignore[no-untyped-def]
    def make(*, threshold: int = 500, enabled: bool = True) -> Settings:
        return settings_factory(
            config_yaml=(
                "filesystem:\n"
                f'  root_dir: "{(tmp_path / "workspace").as_posix()}"\n'
                "context:\n"
                f"  offload_threshold_bytes: {threshold}\n"
                f"  offload_large_results: {'true' if enabled else 'false'}\n"
            )
        )

    return make


def test_offload_passes_through_small_result(offload_settings) -> None:  # type: ignore[no-untyped-def]
    offloader = ResultOffloader(offload_settings(threshold=10_000))

    assert offloader.maybe_offload("小结果", name="t") == "小结果"


def test_offload_writes_file_and_returns_hint(offload_settings) -> None:  # type: ignore[no-untyped-def]
    settings = offload_settings(threshold=200)
    offloader = ResultOffloader(settings)
    big = "内容" * 500

    result = offloader.maybe_offload(big, name="web_search", call_id="call_1234abcd")

    assert "已存为文件" in result
    assert "workspace_read" in result
    # 生成的路径必须在工作区内 —— 否则模型读不回来，卸载等于丢弃
    relative = result.split("`")[1]
    target = settings.root_dir / relative
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == big
    assert offloader.count == 1


def test_offload_disabled(offload_settings) -> None:  # type: ignore[no-untyped-def]
    offloader = ResultOffloader(offload_settings(threshold=10, enabled=False))

    assert offloader.enabled is False
    assert offloader.maybe_offload("很长" * 500, name="t") == "很长" * 500


def test_offload_preview_is_bounded(offload_settings) -> None:  # type: ignore[no-untyped-def]
    offloader = ResultOffloader(offload_settings(threshold=200))

    result = offloader.maybe_offload("x" * 100_000, name="t")

    assert len(result) < 3000


def test_offload_survives_write_failure(offload_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """落盘失败不能让它变成工具失败 —— 退回截断，至少还能用。"""
    offloader = ResultOffloader(offload_settings(threshold=100))
    monkeypatch.setattr(offloader, "_write", _raise_oserror)

    result = offloader.maybe_offload("y" * 5000, name="t")

    assert "已截断" in result


def _raise_oserror(*_args: object, **_kwargs: object) -> Path:
    raise OSError("磁盘满了")


def test_offload_dir_defaults_inside_workspace(offload_settings) -> None:  # type: ignore[no-untyped-def]
    settings = offload_settings()

    offloader = ResultOffloader(settings)

    assert offloader.offload_root.is_relative_to(settings.root_dir)
    assert offloader.offload_root.name == ".offload"


def test_offload_dir_outside_workspace_warns(settings_factory, tmp_path: Path) -> None:
    """落在工作区外时静默会变成「丢弃」—— 必须告警。"""
    settings = settings_factory(
        config_yaml=(
            "filesystem:\n"
            f'  root_dir: "{(tmp_path / "ws").as_posix()}"\n'
            "context:\n"
            f'  offload_dir: "{(tmp_path / "outside").as_posix()}"\n'
        )
    )

    offloader = ResultOffloader(settings)

    assert not offloader.offload_root.is_relative_to(settings.root_dir)


async def test_agent_offloads_large_tool_result(offload_settings) -> None:  # type: ignore[no-untyped-def]
    """端到端：超大工具结果进上下文时已被换成「预览 + 路径」。"""
    from langchain_core.tools import tool

    from doudou.agent.react import ReactAgent
    from doudou.agent.testing import tool_call

    settings = offload_settings(threshold=300)

    @tool
    def big() -> str:
        """返回一大段内容。"""
        return "巨量内容" * 3000

    agent = ReactAgent(
        model=ScriptedChatModel(
            steps=[tool_call("big", {}, call_id="c1"), AIMessage(content="好了")]
        ),
        tools=[big],
        system_prompt="",
        offloader=ResultOffloader(settings),
    )

    result = await agent.ainvoke([HumanMessage(content="给我一大段")])

    tool_messages = [m for m in result.messages if m.type == "tool"]
    assert tool_messages
    assert "已存为文件" in str(tool_messages[0].content)
    assert len(str(tool_messages[0].content)) < 3000


# --------------------------------------------------------------------------- 长期记忆


@pytest.fixture
def memory_settings(settings_factory, tmp_path: Path):  # type: ignore[no-untyped-def]
    def make(*, files: list[str] | None = None, max_chars: int = 8000) -> Settings:
        targets = files if files is not None else [str(tmp_path / "AGENTS.md")]
        # 用 json.dumps 生成列表项：Windows 路径里的反斜杠在 YAML 双引号里
        # 是转义序列，直接拼字符串会写出非法 YAML。
        listing = "".join(f"    - {json.dumps(item)}\n" for item in targets)
        # 空列表必须写在冒号后面（`memory_files: []`）—— 另起一行是非法 YAML。
        memory_yaml = f"  memory_files:\n{listing}" if targets else "  memory_files: []\n"
        return settings_factory(
            config_yaml=(f"agent:\n{memory_yaml}context:\n  max_memory_chars: {max_chars}\n")
        )

    return make


def test_memory_disabled_without_files(memory_settings) -> None:  # type: ignore[no-untyped-def]
    store = MemoryStore(memory_settings(files=[]))

    assert store.enabled is False
    assert store.render() == ""


def test_memory_loads_and_renders(memory_settings, tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("用户偏好中文回复。", encoding="utf-8")
    store = MemoryStore(memory_settings())

    content = store.load()

    assert content.text == "用户偏好中文回复。"
    assert content.chars > 0
    assert MEMORY_SECTION_TITLE in store.render()


def test_memory_merges_multiple_files(memory_settings, tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("第一条", encoding="utf-8")
    (tmp_path / "extra.md").write_text("第二条", encoding="utf-8")
    store = MemoryStore(
        memory_settings(files=[str(tmp_path / "AGENTS.md"), str(tmp_path / "extra.md")])
    )

    text = store.load().text

    assert "第一条" in text and "第二条" in text


def test_memory_truncates_and_flags(memory_settings, tmp_path: Path) -> None:
    """记忆每轮都带进提示词，必须能截断。"""
    (tmp_path / "AGENTS.md").write_text("内容" * 5000, encoding="utf-8")
    store = MemoryStore(memory_settings(max_chars=500))

    content = store.load()

    assert content.truncated is True
    assert content.chars < 800


def test_memory_missing_file_is_not_an_error(memory_settings, tmp_path: Path) -> None:
    store = MemoryStore(memory_settings(files=[str(tmp_path / "nope.md")]))

    assert store.load().empty is True


def test_memory_append_creates_file(memory_settings, tmp_path: Path) -> None:
    store = MemoryStore(memory_settings())

    written = store.append("用户偏好中文回复")

    assert written == "用户偏好中文回复"
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "用户偏好中文回复" in text


def test_memory_append_does_not_lose_existing_content(memory_settings, tmp_path: Path) -> None:
    """只追加不重写：记忆文件是人也会编辑的文本。"""
    (tmp_path / "AGENTS.md").write_text("用户手写的内容\n", encoding="utf-8")
    store = MemoryStore(memory_settings())

    store.append("助手补的一条")

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "用户手写的内容" in text
    assert "助手补的一条" in text


def test_memory_append_without_files_raises(memory_settings) -> None:
    store = MemoryStore(memory_settings(files=[]))

    with pytest.raises(ValueError, match="没有配置记忆文件"):
        store.append("x")


def test_memory_append_rejects_empty(memory_settings) -> None:
    store = MemoryStore(memory_settings())

    with pytest.raises(ValueError, match="为空"):
        store.append("   ")


def test_memory_append_fires_change_callback(memory_settings) -> None:
    """回写后必须让 Agent 换提示词，否则用户会看到「它说记住了但不知道」。"""
    store = MemoryStore(memory_settings())
    fired: list[int] = []
    store.on_change(lambda: fired.append(1))

    store.append("一条事实")

    assert fired == [1]


def test_memory_callback_failure_is_swallowed(memory_settings) -> None:
    store = MemoryStore(memory_settings())

    def boom() -> None:
        raise RuntimeError("回调挂了")

    store.on_change(boom)
    store.append("一条事实")  # 文件已经写成功，不该因为回调失败而报错


def test_memory_enters_system_prompt(memory_settings) -> None:
    from doudou.agent.factory import build_agent

    settings = memory_settings()
    (settings.memory_paths[0]).write_text("用户偏好中文回复。", encoding="utf-8")

    bundle = build_agent(settings, model=ScriptedChatModel(steps=[]))

    assert "用户偏好中文回复" in bundle.system_prompt
    assert MEMORY_SECTION_TITLE in bundle.system_prompt


def test_remember_tool_registered_with_memory(memory_settings) -> None:
    from doudou.tools.builtin import memory as memory_tools

    settings = memory_settings()

    names = [tool.name for tool in memory_tools.build_tools(settings)]

    assert names == ["remember"]


def test_remember_tool_skipped_without_memory(memory_settings) -> None:
    from doudou.tools.builtin import memory as memory_tools

    settings = memory_settings(files=[])

    assert memory_tools.build_tools(settings) == []


def test_remember_tool_needs_approval(memory_settings) -> None:
    """写长期记忆有副作用，默认要人工确认。"""
    from doudou.tools.builtin import memory as memory_tools
    from doudou.tools.decorators import annotations_of

    settings = memory_settings()
    tool = memory_tools.build_tools(settings)[0]

    assert annotations_of(tool).side_effects is True


def test_remember_tool_writes_and_refreshes_prompt(memory_settings) -> None:
    """端到端：调 remember → 文件写入 → 提示词立刻带上新记忆。"""
    from doudou.agent.factory import build_agent

    settings = memory_settings()
    bundle = build_agent(settings, model=ScriptedChatModel(steps=[]))
    remember = next(tool for tool in bundle.tools if tool.name == "remember")
    assert "用户在做 Doudou 这个项目" not in bundle.system_prompt

    result = remember.invoke({"fact": "用户在做 Doudou 这个项目"})

    assert "已记住" in result
    # 回调已触发 → 提示词里必须立刻有了，且 Agent 用的是同一份
    assert "用户在做 Doudou 这个项目" in bundle.system_prompt
    assert bundle.agent.system_prompt == bundle.system_prompt
