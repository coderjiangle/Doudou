"""ReAct 循环：推理 → 调用工具 → 观察 → 继续推理。"""

from __future__ import annotations

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
        "step_start",
        "text_delta",
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
    """单步工具调用数超过上限时只执行前 N 个。"""
    many_calls = [
        {"name": "add", "args": {"a": index, "b": 1}, "id": f"c{index}", "type": "tool_call"}
        for index in range(5)
    ]
    agent = ReactAgent(
        model=ScriptedChatModel(steps=[AIMessage(content="", tool_calls=many_calls)]),
        tools=[add],
        system_prompt="",
        max_steps=1,
        max_tool_calls_per_step=2,
    )

    result = await agent.ainvoke([HumanMessage(content="多调用")])

    assert sum(1 for event in result.events if event.type == "tool_call") == 2


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
