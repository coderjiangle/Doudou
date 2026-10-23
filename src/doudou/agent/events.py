"""Agent 运行时事件：ReAct 循环对外暴露的统一输出格式。

放在 ``agent`` 层是因为它是 Agent 的「产品」：``gateway``、``channels``、``api``
都消费它，但 ``agent`` 不反向依赖它们（见 AGENTS.md 的分层约定）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: 事件类型。
#:
#: - ``run_start``    一次运行开始
#: - ``step_start``   进入新一轮「推理」
#: - ``text_delta``   模型输出的增量文本
#: - ``tool_call``    即将执行某个工具
#: - ``tool_result``  工具执行结束（成功或失败）
#: - ``run_end``      运行结束，``text`` 为最终答复
#: - ``error``        运行以错误终止
EventType = Literal[
    "run_start",
    "step_start",
    "text_delta",
    "tool_call",
    "tool_result",
    "run_end",
    "error",
]


@dataclass(slots=True)
class RunEvent:
    """ReAct 循环产生的单条事件。

    这是一个刻意扁平的结构：CLI 直接渲染它，飞书卡片与 SSE 各自决定怎么用。
    """

    type: EventType
    #: 文本载荷：``text_delta`` 为增量，``run_end`` 为完整答复，``error`` 为错误信息。
    text: str = ""
    #: 第几步（从 1 开始）；``run_start`` 为 0。
    step: int = 0
    thread_id: str | None = None

    # ---- 工具相关 ----
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args: dict[str, Any] | None = None
    #: 工具返回内容（成功为结果，失败为错误说明）。
    result: str | None = None
    tool_ok: bool = True
    #: 工具耗时（秒）。
    tool_elapsed: float | None = None

    # ---- 统计 ----
    #: 累计 token 用量，形如 ``{"input_tokens": 12, "output_tokens": 34}``。
    usage: dict[str, int] = field(default_factory=dict)

    @classmethod
    def run_start(cls, *, thread_id: str | None = None) -> RunEvent:
        return cls(type="run_start", thread_id=thread_id)

    @classmethod
    def step_start(cls, step: int, *, thread_id: str | None = None) -> RunEvent:
        return cls(type="step_start", step=step, thread_id=thread_id)

    @classmethod
    def text_delta(cls, text: str, *, step: int, thread_id: str | None = None) -> RunEvent:
        return cls(type="text_delta", text=text, step=step, thread_id=thread_id)

    @classmethod
    def tool_call(
        cls,
        *,
        step: int,
        name: str,
        call_id: str,
        args: dict[str, Any] | None,
        thread_id: str | None = None,
    ) -> RunEvent:
        return cls(
            type="tool_call",
            step=step,
            tool_name=name,
            tool_call_id=call_id,
            tool_args=args,
            thread_id=thread_id,
        )

    @classmethod
    def tool_result(
        cls,
        *,
        step: int,
        name: str,
        call_id: str,
        result: str,
        ok: bool = True,
        elapsed: float | None = None,
        thread_id: str | None = None,
    ) -> RunEvent:
        return cls(
            type="tool_result",
            step=step,
            tool_name=name,
            tool_call_id=call_id,
            result=result,
            tool_ok=ok,
            tool_elapsed=elapsed,
            thread_id=thread_id,
        )

    @classmethod
    def run_end(
        cls,
        text: str,
        *,
        step: int,
        usage: dict[str, int] | None = None,
        thread_id: str | None = None,
    ) -> RunEvent:
        return cls(type="run_end", text=text, step=step, usage=usage or {}, thread_id=thread_id)

    @classmethod
    def error(cls, message: str, *, step: int = 0, thread_id: str | None = None) -> RunEvent:
        return cls(type="error", text=message, step=step, thread_id=thread_id)


@dataclass(slots=True)
class ToolOutcome:
    """内部用：一次工具调用的完整结果。"""

    call_id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    content: str = ""
    ok: bool = True
    elapsed: float | None = None


__all__ = ["EventType", "RunEvent", "ToolOutcome"]
