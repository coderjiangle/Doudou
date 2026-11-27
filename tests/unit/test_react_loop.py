"""ReAct 循环：推理 → 调用工具 → 观察 → 继续推理。"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from doudou.agent.react import (
    ApprovalHook,
    ReactAgent,
    RunResult,
    ToolApprovalRequest,
    _finalize_tool_calls,
    _ModelTurn,
    build_messages,
)
from doudou.agent.testing import ScriptedChatModel, summarize_last_tool_result, tool_call
from doudou.tools.decorators import annotate


@tool
def add(a: int, b: int) -> int:
    """把两个整数相加。"""
    return a + b


@tool
def boom() -> str:
    """总是抛异常，用于测试单步失败不中断循环。"""
    raise RuntimeError("工具炸了")


def make_agent(
    steps: list[object],
    *,
    tools: list[BaseTool] | None = None,
    interrupt_on: dict[str, bool] | None = None,
    approval_hook: ApprovalHook | None = None,
    max_steps: int = 8,
) -> ReactAgent:
    return ReactAgent(
        model=ScriptedChatModel(steps=list(steps)),
        tools=tools if tools is not None else [add, boom],
        system_prompt="你是测试助手。",
        max_steps=max_steps,
        interrupt_on=interrupt_on,
        approval_hook=approval_hook,
    )


# --------------------------------------------------------------------------- 正常路径


async def test_tool_call_then_final_answer() -> None:
    """一次工具往返：事件顺序、工具结果回填、系统提示词注入。"""
    agent = make_agent(
        [tool_call("add", {"a": 1, "b": 2}, call_id="call_add"), summarize_last_tool_result()]
    )

    result: RunResult = await agent.ainvoke([HumanMessage(content="1+2 等于几？")])

    assert result.ok
    assert result.text == "工具返回：3"
    assert result.steps == 2

    assert [event.type for event in result.events] == [
        "run_start",
        "step_start",
        "tool_call",
        "tool_result",
        "step_end",
        "step_start",
        "text_delta",
        "step_end",
        "run_end",
    ]

    call = next(event for event in result.events if event.type == "tool_call")
    assert call.tool_name == "add"
    assert call.tool_args == {"a": 1, "b": 2}

    outcome = next(event for event in result.events if event.type == "tool_result")
    assert outcome.tool_ok is True
    assert outcome.result == "3"

    # 系统提示词自动插到最前面，且历史里含 ToolMessage。
    assert isinstance(result.messages[0], SystemMessage)
    assert any(isinstance(message, ToolMessage) for message in result.messages)


async def test_text_streaming_emits_deltas() -> None:
    """最终答复以 text_delta 流式吐出，run_end 里是完整文本。"""
    agent = make_agent([AIMessage(content="你好呀")])

    deltas = [
        event.text
        async for event in agent.astream([HumanMessage(content="你好")])
        if event.type == "text_delta"
    ]

    assert "".join(deltas) == "你好呀"


async def test_no_tools_does_not_bind() -> None:
    """没有工具时不调用 bind_tools，避免部分 provider 在空工具列表上报错。"""
    model = ScriptedChatModel(steps=[AIMessage(content="ok")])
    agent = ReactAgent(model=model, tools=[], system_prompt="")

    result = await agent.ainvoke([HumanMessage(content="嗨")])

    assert result.text == "ok"
    assert model.bound_tool_names == []


async def test_existing_system_message_is_not_duplicated() -> None:
    """调用方自己给了 SystemMessage 时不再插入。"""
    agent = make_agent([AIMessage(content="ok")])

    result = await agent.ainvoke([SystemMessage(content="自定义"), HumanMessage(content="嗨")])

    assert [message.content for message in result.messages].count("自定义") == 1
    assert result.messages[0].content == "自定义"


# --------------------------------------------------------------------------- 失败处理


async def test_unknown_tool_feeds_error_back_and_continues() -> None:
    """调用不存在的工具：错误回填给模型，循环继续而不是中断。"""
    agent = make_agent([tool_call("nope", {}, call_id="c1"), AIMessage(content="我换个办法")])

    result = await agent.ainvoke([HumanMessage(content="用不存在的工具")])

    assert result.ok
    assert result.text == "我换个办法"

    outcome = next(event for event in result.events if event.type == "tool_result")
    assert outcome.tool_ok is False
    assert "不存在名为 'nope' 的工具" in (outcome.result or "")
    assert "add" in (outcome.result or "")  # 提示里带上了可用工具


async def test_tool_exception_becomes_error_message() -> None:
    """工具抛异常：转成错误结果，不把异常抛给用户。"""
    agent = make_agent([tool_call("boom", {}, call_id="c1"), AIMessage(content="失败了")])

    result = await agent.ainvoke([HumanMessage(content="炸一个")])

    assert result.ok
    outcome = next(event for event in result.events if event.type == "tool_result")
    assert outcome.tool_ok is False
    assert "RuntimeError" in (outcome.result or "")
    assert "工具炸了" in (outcome.result or "")


async def test_max_steps_raises_error_event() -> None:
    """模型一直要工具：达到步数上限后以 error 事件收尾。"""
    agent = make_agent(
        [
            tool_call("add", {"a": 1, "b": 1}, call_id="c1"),
            tool_call("add", {"a": 2, "b": 2}, call_id="c2"),
        ],
        max_steps=2,
    )

    result = await agent.ainvoke([HumanMessage(content="死循环")])

    assert result.ok is False
    assert result.error is not None and "最大步数 2" in result.error
    assert result.steps == 2
    assert sum(1 for event in result.events if event.type == "tool_call") == 2


async def test_tool_calls_truncated_per_step() -> None:
    """单步工具调用数超过上限时只执行前 N 个，且被丢弃的调用也要有应答。

    必须跑到第二轮：AIMessage 带着全部 tool_calls，缺了 ToolMessage 的 id
    会在下一次请求真实 provider 时 400，只跑一步测不到这个问题。
    """
    many_calls = [
        {"name": "add", "args": {"a": index, "b": 1}, "id": f"c{index}", "type": "tool_call"}
        for index in range(5)
    ]
    agent = ReactAgent(
        model=ScriptedChatModel(
            steps=[AIMessage(content="", tool_calls=many_calls), AIMessage(content="完成")]
        ),
        tools=[add],
        system_prompt="",
        max_steps=3,
        max_tool_calls_per_step=2,
    )

    result = await agent.ainvoke([HumanMessage(content="多调用")])

    assert result.ok
    assert sum(1 for event in result.events if event.type == "tool_call") == 2

    answered = {m.tool_call_id: m for m in result.messages if isinstance(m, ToolMessage)}
    requested = {c["id"] for m in result.messages if isinstance(m, AIMessage) for c in m.tool_calls}
    assert requested == answered.keys()
    assert [answered[f"c{i}"].status for i in range(5)] == ["success"] * 2 + ["error"] * 3


def _ordered_tools(log: list[str]) -> list[BaseTool]:
    """一个只读、一个有副作用的异步工具，调用起止都记进 ``log``。"""

    async def _run(kind: str, label: str) -> str:
        log.append(f"start {label}")
        await asyncio.sleep(0.02)
        log.append(f"end {label}")
        return f"{kind}:{label}"

    @tool
    async def read_item(label: str) -> str:
        """只读工具。"""
        return await _run("read", label)

    @tool
    async def write_item(label: str) -> str:
        """有副作用的工具。"""
        return await _run("write", label)

    return [
        annotate(read_item, group="test", side_effects=False),
        annotate(write_item, group="test", side_effects=True),
    ]


async def test_side_effect_tools_run_serially_as_barriers() -> None:
    """只读调用并发；有副作用的调用是栅栏，前后都不与其他调用重叠，结果仍按原顺序。"""
    log: list[str] = []
    calls = [
        {"name": name, "args": {"label": label}, "id": label, "type": "tool_call"}
        for name, label in [
            ("read_item", "r1"),
            ("read_item", "r2"),
            ("write_item", "w1"),
            ("write_item", "w2"),
            ("read_item", "r3"),
        ]
    ]
    agent = ReactAgent(
        model=ScriptedChatModel(
            steps=[AIMessage(content="", tool_calls=calls), AIMessage(content="完成")]
        ),
        tools=_ordered_tools(log),
        system_prompt="",
        max_steps=3,
    )

    result = await agent.ainvoke([HumanMessage(content="读写混合")])

    assert result.ok
    # 读 r1/r2 并发：两者都启动后才有任何一个结束。
    assert sorted(log[:2]) == ["start r1", "start r2"]
    # 写 w1、w2 严格串行，且都在前面的读结束之后、后面的读开始之前。
    assert log[4:] == ["start w1", "end w1", "start w2", "end w2", "start r3", "end r3"]
    # 回填给模型的 ToolMessage 与调用顺序一致。
    tool_ids = [m.tool_call_id for m in result.messages if isinstance(m, ToolMessage)]
    assert tool_ids == ["r1", "r2", "w1", "w2", "r3"]


# --------------------------------------------------------------------------- 人工确认


async def test_approval_granted_runs_tool() -> None:
    seen: list[ToolApprovalRequest] = []

    async def approve(request: ToolApprovalRequest) -> bool:
        seen.append(request)
        return True

    agent = make_agent(
        [tool_call("add", {"a": 2, "b": 3}, call_id="c1"), summarize_last_tool_result()],
        interrupt_on={"add": True},
        approval_hook=approve,
    )

    result = await agent.ainvoke([HumanMessage(content="算一下")])

    assert seen and seen[0].tool_name == "add"
    assert result.text == "工具返回：5"


async def test_approval_denied_skips_tool() -> None:
    async def reject(request: ToolApprovalRequest) -> bool:
        return False

    agent = make_agent(
        [tool_call("add", {"a": 2, "b": 3}, call_id="c1"), AIMessage(content="好的，不执行")],
        interrupt_on={"add": True},
        approval_hook=reject,
    )

    result = await agent.ainvoke([HumanMessage(content="算一下")])

    outcome = next(event for event in result.events if event.type == "tool_result")
    assert outcome.tool_ok is False
    assert "拒绝" in (outcome.result or "")


async def test_wildcard_approval_applies_to_all_tools() -> None:
    async def reject(request: ToolApprovalRequest) -> bool:
        return False

    agent = make_agent(
        [tool_call("add", {"a": 1, "b": 1}, call_id="c1"), AIMessage(content="停")],
        interrupt_on={"*": True},
        approval_hook=reject,
    )

    result = await agent.ainvoke([HumanMessage(content="算一下")])

    outcome = next(event for event in result.events if event.type == "tool_result")
    assert outcome.tool_ok is False


async def test_missing_approval_hook_rejects_with_hint() -> None:
    """需要确认但没提供回调：拒绝执行并说明原因，而不是静默放行。"""
    agent = make_agent(
        [tool_call("add", {"a": 1, "b": 1}, call_id="c1"), AIMessage(content="知道了")],
        interrupt_on={"add": True},
        approval_hook=None,
    )

    result = await agent.ainvoke([HumanMessage(content="算一下")])

    outcome = next(event for event in result.events if event.type == "tool_result")
    assert outcome.tool_ok is False
    assert "未提供确认回调" in (outcome.result or "")


# --------------------------------------------------------------------------- 参数解析


def test_finalize_tool_calls_parses_args() -> None:
    calls, errors = _finalize_tool_calls(
        {0: {"id": "a", "name": "add", "args": '{"a": 1, "b": 2}'}}, []
    )
    assert errors == {}
    assert calls == [{"name": "add", "args": {"a": 1, "b": 2}, "id": "a", "type": "tool_call"}]


def test_finalize_tool_calls_reports_bad_json() -> None:
    calls, errors = _finalize_tool_calls({0: {"id": "a", "name": "add", "args": '{"a": '}}, [])
    assert list(errors) == ["a"]
    assert calls[0]["args"] == {}


def test_finalize_tool_calls_rejects_non_object_args() -> None:
    calls, errors = _finalize_tool_calls({0: {"id": "a", "name": "add", "args": "[1, 2]"}}, [])
    assert "必须是 JSON 对象" in errors["a"]
    assert calls[0]["args"] == {}


def test_finalize_tool_calls_drops_nameless_fragment() -> None:
    calls, _ = _finalize_tool_calls({0: {"id": "a", "name": None, "args": "{}"}}, [])
    assert calls == []


def test_finalize_tool_calls_falls_back_to_direct_calls() -> None:
    """provider 直接给完整 tool_calls（无 chunks）时也能解析。"""
    calls, errors = _finalize_tool_calls({}, [{"name": "add", "args": {"a": 1}, "id": "x"}])
    assert errors == {}
    assert calls[0]["name"] == "add"
    assert calls[0]["id"] == "x"


async def test_arg_error_short_circuits_tool_execution() -> None:
    """参数解析失败的工具调用不进工具函数，直接返回可读错误。"""
    agent = make_agent([AIMessage(content="ok")])
    turn = _ModelTurn(arg_errors={"c1": "非法 JSON：Expecting value"})

    outcome = await agent._execute_tool({"name": "add", "args": {}, "id": "c1"}, step=1, turn=turn)

    assert outcome.ok is False
    assert "参数无法解析" in outcome.content


# --------------------------------------------------------------------------- 消息组装


def test_build_messages_order() -> None:
    messages = build_messages("sys", [HumanMessage(content="旧")], "新")
    assert [type(message).__name__ for message in messages] == [
        "SystemMessage",
        "HumanMessage",
        "HumanMessage",
    ]


def test_finalize_tool_calls_merges_index_order() -> None:
    """按 index 排序，保证工具结果与调用顺序一致。"""
    calls, _ = _finalize_tool_calls(
        {
            1: {"id": "b", "name": "add", "args": json.dumps({"a": 2, "b": 2})},
            0: {"id": "a", "name": "add", "args": json.dumps({"a": 1, "b": 1})},
        },
        [],
    )
    assert [call["id"] for call in calls] == ["a", "b"]


@pytest.mark.parametrize(
    ("json_fragment", "expected"),
    [("", {}), ('{"a": 1}', {"a": 1})],
)
def test_finalize_tool_calls_empty_and_valid(json_fragment: str, expected: dict) -> None:
    calls, errors = _finalize_tool_calls({0: {"id": "a", "name": "add", "args": json_fragment}}, [])
    assert errors == {}
    assert calls[0]["args"] == expected


# --------------------------------------------------------------------------- 摘要合并与人设锚点


async def test_summary_system_message_preserves_system_prompt() -> None:
    """历史摘要占用首条 SystemMessage 时，仍保留 agent.system_prompt 且合并成单条。"""
    from doudou.agent.context import SUMMARY_PREFIX

    seen: list[list] = []

    def _capture(messages: list) -> AIMessage:
        seen.append(list(messages))
        return AIMessage(content="汪！记得的")

    model = ScriptedChatModel(steps=[_capture])
    agent = ReactAgent(model=model, tools=[], system_prompt="你是豆豆，中华田园犬。")

    result = await agent.ainvoke(
        [
            SystemMessage(content=f"{SUMMARY_PREFIX} 之前爸爸问过天气。"),
            HumanMessage(content="那今天呢？"),
        ]
    )

    assert result.ok
    assert len(seen) == 1
    system_messages = [msg for msg in seen[0] if isinstance(msg, SystemMessage)]
    assert len(system_messages) == 1
    assert "你是豆豆，中华田园犬。" in str(system_messages[0].content)
    assert SUMMARY_PREFIX in str(system_messages[0].content)


async def test_persona_reminder_only_applied_at_turn_interval() -> None:
    """人设锚点按轮次间隔插入：不足间隔不插，达到间隔才附着在最后一条用户消息上。"""
    seen: list[list] = []

    def _capture(messages: list) -> AIMessage:
        seen.append(list(messages))
        return AIMessage(content="汪")

    agent = ReactAgent(
        model=ScriptedChatModel(steps=[_capture, _capture, _capture]),
        tools=[],
        system_prompt="你是豆豆。",
        persona_reminder="保持豆豆口吻",
        persona_reminder_interval=3,
    )

    # 第 1 轮（1 < 3）：不附带提醒
    await agent.ainvoke([HumanMessage(content="第一问")])
    assert "保持豆豆口吻" not in str(seen[0][-1].content)

    # 第 2 轮（2 < 3）：不附带提醒
    await agent.ainvoke(
        [
            HumanMessage(content="第一问"),
            AIMessage(content="汪"),
            HumanMessage(content="第二问"),
        ]
    )
    assert "保持豆豆口吻" not in str(seen[1][-1].content)

    # 第 3 轮（3 >= 3）：附带提醒；返回值中的 produced 不被污染
    produced: list = []
    events = [
        event
        async for event in agent.astream(
            [
                HumanMessage(content="第一问"),
                AIMessage(content="汪"),
                HumanMessage(content="第二问"),
                AIMessage(content="汪"),
                HumanMessage(content="第三问"),
            ],
            messages_out=produced,
        )
    ]
    assert events[-1].type == "run_end"
    assert "第三问" in str(seen[2][-1].content)
    assert "[人设提醒] 保持豆豆口吻" in str(seen[2][-1].content)
    assert all("人设提醒" not in str(msg.content) for msg in produced)


async def test_persona_reminder_supports_multimodal_and_summary() -> None:
    """多模态 HumanMessage 与摘要场景也能正确附带人设锚点，并可无损剥离展示文本。"""
    from doudou.agent.context import SUMMARY_PREFIX
    from doudou.agent.messages import (
        append_time_context,
        build_time_context,
        strip_display_annotations_message,
    )

    seen: list[list] = []

    def _capture(messages: list) -> AIMessage:
        seen.append(list(messages))
        return AIMessage(content="汪")

    agent = ReactAgent(
        model=ScriptedChatModel(steps=[_capture]),
        tools=[],
        system_prompt="你是豆豆。",
        persona_reminder="保持豆豆口吻",
        persona_reminder_interval=10,  # 即使间隔大，有摘要且窗口内无锚点时也应注入
    )

    await agent.ainvoke(
        [
            SystemMessage(content=f"{SUMMARY_PREFIX} 早期对话"),
            HumanMessage(content=[{"type": "text", "text": "看看这张图"}]),
        ]
    )

    last_msg = seen[0][-1]
    last_content = last_msg.content
    assert isinstance(last_content, list)
    assert last_content[0]["text"] == "看看这张图"
    assert "[人设提醒] 保持豆豆口吻" in last_content[-1]["text"]

    stripped = strip_display_annotations_message(last_msg)
    assert stripped.content == [{"type": "text", "text": "看看这张图"}]

    # 同时带 [时间上下文] 与 [人设提醒] 时也能完整剥离
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 17, 11, 50, tzinfo=UTC)
    time_ctx = build_time_context(
        last_active_at=now - timedelta(days=2),
        timezone_name="Asia/Shanghai",
        gap_hours=4.0,
        now=now,
    )
    assert "2026-11-17 19:50" in time_ctx
    assert "距上次对话已过 2 天" in time_ctx

    anchored = append_time_context(HumanMessage(content="今天什么安排"), time_ctx)
    assert "[时间上下文]" in str(anchored.content)
    assert strip_display_annotations_message(anchored).content == "今天什么安排"


async def test_step_and_run_timing_breakdown_separates_llm_tools_and_approval() -> None:
    """并发只读工具按并集算墙钟耗时，且人工确认等待不计入工具/LLM 耗时。"""

    @tool
    async def slow_read(label: str) -> str:
        """只读慢工具，测试并发墙钟合并。"""
        await asyncio.sleep(0.05)
        return label

    @tool
    async def guarded_write(path: str) -> str:
        """写工具，测试确认等待与执行耗时分离。"""
        await asyncio.sleep(0.02)
        return f"wrote:{path}"

    annotate(slow_read, group="files", side_effects=False)
    annotate(guarded_write, group="files", side_effects=True)

    async def delayed_approve(_req: ToolApprovalRequest) -> bool:
        await asyncio.sleep(0.06)
        return True

    agent = make_agent(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "slow_read", "args": {"label": "a"}, "id": "c1", "type": "tool_call"},
                    {"name": "slow_read", "args": {"label": "b"}, "id": "c2", "type": "tool_call"},
                ],
            ),
            tool_call("guarded_write", {"path": "x.txt"}, call_id="c3"),
            summarize_last_tool_result(),
        ],
        tools=[slow_read, guarded_write],
        interrupt_on={"guarded_write": True},
        approval_hook=delayed_approve,
    )

    result = await agent.ainvoke([HumanMessage(content="测耗时")])

    assert result.ok
    assert result.llm_elapsed is not None and result.llm_elapsed >= 0.0
    assert result.ttft is not None and result.ttft >= 0.0
    assert result.tool_elapsed is not None
    assert result.approval_elapsed is not None and result.approval_elapsed >= 0.04

    step_ends = [event for event in result.events if event.type == "step_end"]
    assert [event.step for event in step_ends] == [1, 2, 3]
    # 第 1 步两个 0.05s 工具并发执行：总墙钟应明显小于串行之和 (0.10s)
    step1_tool_results = [
        event for event in result.events if event.type == "tool_result" and event.step == 1
    ]
    assert len(step1_tool_results) == 2
    sum_single = sum(event.tool_elapsed or 0.0 for event in step1_tool_results)
    assert step_ends[0].tool_elapsed is not None
    assert step_ends[0].tool_elapsed <= sum_single + 0.005
    assert step_ends[0].tool_elapsed < 0.095
    # 第 2 步确认等待 0.06s 应归入 approval_elapsed，不污染 tool_elapsed
    assert step_ends[1].approval_elapsed is not None and step_ends[1].approval_elapsed >= 0.04
    assert step_ends[1].tool_elapsed is not None and step_ends[1].tool_elapsed < 0.05
    # 第 3 步无工具，tool_elapsed == 0.0
    assert step_ends[2].tool_elapsed == 0.0
