"""运行轨迹：一次运行一条记录，步骤明细存 JSON。

结构是一棵树（运行 → step → 工具调用），**本地落库**（``run_traces`` 表），
配合自建的统计页 / 回放接口（``/traces``、``/stats``）。这里曾经把这棵树发给
第三方追踪服务（Langfuse）；现在不再上报，也不依赖任何外部服务。

数据来源仍是 ReAct 循环的 :class:`~doudou.agent.events.RunEvent` 流。
观察者是**回调式**的（``on_run_*`` 在不同时刻），所以这里把事件攒到运行结束，
在 ``on_run_end`` 里一次性聚成步骤树并落库 —— 好处是运行中途异常也不会留下悬挂状态。

三项职责分得很清，别混：

* ``audit_events``（审计）—— 安全视角：谁调了什么工具、参数摘要、是否经人确认；
* ``usage_daily``（成本）—— 账本：按天 × 会话的 token 与费用；
* ``run_traces``（本模块）—— 回放视角：**每一步说了什么、调了哪些工具、结果如何**。

轨迹是**派生数据**（对话历史 + 审计足以重建），因此可以放心按条数压制增长、
截断文本 —— 它不是任何东西的唯一真相源。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from doudou.agent.events import RunEvent
from doudou.observability.logging import get_logger
from doudou.observability.observers import BaseObserver, RunContext, RunSummary
from doudou.security.secrets import redact

logger = get_logger(__name__)

#: 一次运行最多保留多少条事件用于轨迹（防止超长运行把内存吃掉）。
MAX_BUFFERED_EVENTS = 500
#: 工具参数/结果与步骤文本、输入输出的默认截断长度。
MAX_PAYLOAD_CHARS = 1000


@dataclass(slots=True)
class TraceRecord:
    """一次运行的轨迹。

    ``steps_detail`` 是**按 step 聚合**的列表（每步的文本 + 工具调用），
    存进库时序列化成 JSON；其余标量列用于列表与聚合查询。
    """

    run_id: str
    thread_id: str
    channel: str = ""
    conversation_id: str = ""
    user_id: str | None = None
    model: str = ""
    ok: bool = True
    error: str | None = None
    #: 步数与各类计数（来自 :class:`~doudou.observability.observers.RunSummary`）。
    steps: int = 0
    tool_calls: int = 0
    approvals: int = 0
    rejections: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed: float = 0.0
    llm_elapsed: float | None = None
    ttft: float | None = None
    tool_elapsed: float | None = None
    approval_elapsed: float | None = None
    input_preview: str = ""
    output_preview: str = ""
    #: 按 step 聚合的明细（JSON 列）。
    steps_detail: list[dict[str, Any]] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def other_elapsed(self) -> float | None:
        """总耗时中扣除 LLM / 工具 / 人工等待后的其余开销（锁、压缩、落库等）。"""
        if self.llm_elapsed is None:
            return None
        remainder = (
            self.elapsed
            - self.llm_elapsed
            - (self.tool_elapsed or 0.0)
            - (self.approval_elapsed or 0.0)
        )
        return round(max(0.0, remainder), 3)

    def as_summary(self) -> dict[str, Any]:
        """列表用：不含 ``steps_detail``（那可能很大）。"""
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "channel": self.channel,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "model": self.model,
            "ok": self.ok,
            "error": self.error,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "approvals": self.approvals,
            "rejections": self.rejections,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "elapsed": self.elapsed,
            "llm_elapsed": self.llm_elapsed,
            "ttft": self.ttft,
            "tool_elapsed": self.tool_elapsed,
            "approval_elapsed": self.approval_elapsed,
            "other_elapsed": self.other_elapsed,
            "input_preview": self.input_preview,
            "output_preview": self.output_preview,
            "started_at": self.started_at.isoformat(),
        }

    def as_dict(self) -> dict[str, Any]:
        """详情用：含步骤明细。"""
        return {**self.as_summary(), "steps_detail": [dict(item) for item in self.steps_detail]}


class TraceStore(Protocol):
    """轨迹的落点。``memory`` 实现用于无数据库的部署与测试。"""

    async def append(self, record: TraceRecord) -> None:
        """写入一次运行的轨迹（``run_id`` 唯一）。"""
        ...

    async def append_many(self, records: list[TraceRecord]) -> None:
        """批量写入（一次提交）。见 ``buffering.py``。"""
        ...

    async def list_recent(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        thread_id: str | None = None,
        channel: str | None = None,
        since: datetime | None = None,
        only_failed: bool = False,
        include_timing: bool = False,
    ) -> list[TraceRecord]: ...

    async def count(
        self,
        *,
        thread_id: str | None = None,
        channel: str | None = None,
        since: datetime | None = None,
        only_failed: bool = False,
    ) -> int:
        """符合条件的总条数（分页控件用）。"""
        ...

    async def get(self, run_id: str) -> TraceRecord | None: ...

    async def delete_for_thread(self, thread_id: str) -> int:
        """删除某条会话的全部轨迹，返回删除条数（删会话时调用）。"""
        ...


class MemoryTraceStore:
    """进程内轨迹（有容量上限的环形缓冲）。

    上限是必须的：轨迹带正文，不设上限的进程内列表就是一条缓慢的内存泄漏。
    超出后丢最旧的 —— 排障看的总是最近的。
    """

    def __init__(self, *, max_entries: int = 2000) -> None:
        self.max_entries = max(1, max_entries)
        self._records: dict[str, TraceRecord] = {}

    async def append(self, record: TraceRecord) -> None:
        self._records.pop(record.run_id, None)  # 重新插入以维持「最近在最前」
        self._records[record.run_id] = record
        while len(self._records) > self.max_entries:
            self._records.pop(next(iter(self._records)), None)

    async def append_many(self, records: list[TraceRecord]) -> None:
        for record in records:
            await self.append(record)

    async def list_recent(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        thread_id: str | None = None,
        channel: str | None = None,
        since: datetime | None = None,
        only_failed: bool = False,
        include_timing: bool = False,
    ) -> list[TraceRecord]:
        items = self._matching(
            thread_id=thread_id, channel=channel, since=since, only_failed=only_failed
        )
        items.sort(key=lambda item: (item.started_at, item.run_id), reverse=True)
        start = max(0, offset)
        return items[start : start + max(1, limit)]

    async def count(
        self,
        *,
        thread_id: str | None = None,
        channel: str | None = None,
        since: datetime | None = None,
        only_failed: bool = False,
    ) -> int:
        return len(
            self._matching(
                thread_id=thread_id, channel=channel, since=since, only_failed=only_failed
            )
        )

    def _matching(
        self,
        *,
        thread_id: str | None,
        channel: str | None,
        since: datetime | None,
        only_failed: bool,
    ) -> list[TraceRecord]:
        return [
            item
            for item in self._records.values()
            if _matches(
                item, thread_id=thread_id, channel=channel, since=since, only_failed=only_failed
            )
        ]

    async def get(self, run_id: str) -> TraceRecord | None:
        return self._records.get(run_id)

    async def delete_for_thread(self, thread_id: str) -> int:
        keys = [key for key, record in self._records.items() if record.thread_id == thread_id]
        for key in keys:
            self._records.pop(key, None)
        return len(keys)


def _matches(
    record: TraceRecord,
    *,
    thread_id: str | None,
    channel: str | None,
    since: datetime | None,
    only_failed: bool,
) -> bool:
    if thread_id and record.thread_id != thread_id:
        return False
    if channel and record.channel != channel:
        return False
    if since is not None and record.started_at < since:
        return False
    return not (only_failed and record.ok)


class TraceObserver(BaseObserver):
    """攒事件 → 运行结束时聚成步骤树并写入 :class:`TraceStore`。"""

    name = "trace"

    def __init__(
        self,
        store: TraceStore,
        *,
        model: str = "",
        max_text_chars: int = MAX_PAYLOAD_CHARS,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self.max_text_chars = max(0, max_text_chars)
        self._new_id = id_factory or (lambda: uuid4().hex[:16])
        self._runs: dict[str, TraceRecord] = {}
        self._events: dict[str, list[RunEvent]] = {}

    async def on_run_start(self, context: RunContext) -> None:
        self._runs[context.thread_id] = TraceRecord(
            run_id=self._new_id(),
            thread_id=context.thread_id,
            channel=context.channel,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
            model=self.model,
            input_preview=_clip_text(context.text, self.max_text_chars),
        )
        self._events[context.thread_id] = []

    async def on_event(self, context: RunContext, event: RunEvent) -> None:
        buffer = self._events.get(context.thread_id)
        if buffer is None:
            buffer = self._events[context.thread_id] = []
        if len(buffer) < MAX_BUFFERED_EVENTS:
            buffer.append(event)
        elif len(buffer) == MAX_BUFFERED_EVENTS:
            logger.warning(
                "trace.events_truncated", thread_id=context.thread_id, limit=MAX_BUFFERED_EVENTS
            )
            buffer.append(RunEvent(type="error", text="事件过多，轨迹记录已截断"))

    async def on_run_end(self, context: RunContext, summary: RunSummary) -> None:
        record = self._runs.pop(context.thread_id, None)
        events = self._events.pop(context.thread_id, [])
        if record is None:
            # 没有 run_start（事件流被截断）：不造一条无头记录，只记一条日志。
            logger.debug("trace.missing_run_start", thread_id=context.thread_id)
            return

        steps = _group_steps(events, max_chars=self.max_text_chars)
        step_llm, step_ttft, step_tool, step_approval = summarize_steps_timing(steps)
        record.ok = summary.ok
        record.error = _clip_text(summary.error or "", self.max_text_chars) or None
        record.steps = summary.steps
        record.tool_calls = summary.tool_calls
        record.approvals = summary.approvals
        record.rejections = summary.rejections
        record.input_tokens = int(summary.usage.get("input_tokens", 0) or 0)
        record.output_tokens = int(summary.usage.get("output_tokens", 0) or 0)
        record.elapsed = round(summary.elapsed, 3)
        record.llm_elapsed = (
            _round_opt(summary.llm_elapsed, 3) if summary.llm_elapsed is not None else step_llm
        )
        record.ttft = _round_opt(summary.ttft, 3) if summary.ttft is not None else step_ttft
        record.tool_elapsed = (
            _round_opt(summary.tool_elapsed, 3) if summary.tool_elapsed is not None else step_tool
        )
        record.approval_elapsed = (
            _round_opt(summary.approval_elapsed, 3)
            if summary.approval_elapsed is not None
            else step_approval
        )
        record.output_preview = _clip_text(_final_text(events, steps), self.max_text_chars)
        record.steps_detail = steps
        record.created_at = datetime.now(UTC)

        # store 是异步的（与审计/用量一致），不需要下放线程。
        try:
            await self.store.append(record)
        except Exception as exc:
            # 观察者那层还有一道 try/except；这里多记一条带 run 的日志更利于排查。
            logger.warning("trace.persist_failed", run_id=record.run_id, error=str(exc))

    async def aclose(self) -> None:
        self._runs.clear()
        self._events.clear()


def _final_text(events: Sequence[RunEvent], steps: Sequence[dict[str, Any]]) -> str:
    """输出的最终答复：优先取 ``run_end`` 的文本，退回最后一步的文本。"""
    for event in reversed(events):
        if event.type == "run_end" and event.text:
            return event.text
    if steps:
        return str(steps[-1].get("text", ""))
    return ""


def _group_steps(events: Sequence[RunEvent], *, max_chars: int) -> list[dict[str, Any]]:
    """把事件按 step 聚成「这一步说了什么、调了哪些工具」。

    工具结果按 ``tool_call_id`` 合并回它的调用 —— 分开列会让人误以为
    结果是独立发生的事。

    工具参数**先打码再截断**（与审计一致）：轨迹表不是密钥仓库。
    """
    steps: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    pending: dict[str, dict[str, Any]] = {}

    def bucket(index: int) -> dict[str, Any]:
        item = steps.get(index)
        if item is None:
            item = {
                "step": index,
                "text": "",
                "llm_elapsed": None,
                "ttft": None,
                "tool_elapsed": None,
                "approval_elapsed": None,
                "tools": [],
            }
            steps[index] = item
            order.append(index)
        return item

    for event in events:
        if event.step < 1:
            # ``run_start`` 的 step 是 0；它不属于任何一步，别为它造一个空 step。
            continue

        step = bucket(event.step)
        if event.type == "text_delta":
            step["text"] += event.text
        elif event.type == "tool_call":
            call: dict[str, Any] = {
                "name": event.tool_name or "",
                "call_id": event.tool_call_id or "",
                "step": event.step,
                "args": _clip(redact(event.tool_args or {}), max_chars),
                "ok": None,
                "approved": None,
                "approval_elapsed": None,
                "result": "",
                "elapsed": None,
                "error": None,
            }
            step["tools"].append(call)
            if call["call_id"]:
                pending[call["call_id"]] = call
        elif event.type == "approval":
            target = pending.get(event.tool_call_id or "")
            if target is not None:
                target["approved"] = event.tool_ok
                if event.approval_elapsed is not None:
                    target["approval_elapsed"] = _round_opt(event.approval_elapsed, 4)
        elif event.type == "tool_result":
            target = pending.get(event.tool_call_id or "")
            if target is None:
                # 没配对的 tool_call（事件流被截断）：照样记一条，别丢信息。
                target = {
                    "name": event.tool_name or "",
                    "call_id": event.tool_call_id or "",
                    "step": event.step,
                    "args": {},
                    "ok": None,
                    "approved": None,
                    "approval_elapsed": None,
                    "result": "",
                    "elapsed": None,
                    "error": None,
                }
                step["tools"].append(target)
            target["ok"] = event.tool_ok
            target["result"] = _clip_text(event.result or "", max_chars)
            target["elapsed"] = event.tool_elapsed
            if not event.tool_ok:
                target["error"] = _clip_text(event.result or "", min(max_chars, 300))
        elif event.type == "step_end":
            step["llm_elapsed"] = _round_opt(event.llm_elapsed, 4)
            step["ttft"] = _round_opt(event.ttft, 4)
            step["tool_elapsed"] = _round_opt(event.tool_elapsed, 4)
            step["approval_elapsed"] = _round_opt(event.approval_elapsed, 4)
        elif event.type == "error":
            step["text"] = step["text"] or event.text

    for index in order:
        steps[index]["text"] = _clip_text(steps[index]["text"], max_chars)
    return [steps[index] for index in order]


def _clip_text(value: str, limit: int) -> str:
    """截断进入轨迹的文本。轨迹是给人看的回放，不是数据备份。"""
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return f"{value[:limit]}…（截断，共 {len(value)} 字）"


def _clip(value: Any, limit: int) -> Any:
    """工具参数是任意 JSON：递归截断其中的字符串，保留结构（不打散 JSON）。"""
    if isinstance(value, str):
        return _clip_text(value, limit)
    if isinstance(value, dict):
        return {key: _clip(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip(item, limit) for item in value]
    return value


def _round_opt(value: float | None, digits: int = 4) -> float | None:
    return round(float(value), digits) if value is not None else None


def summarize_steps_timing(
    steps_detail: Sequence[Mapping[str, Any]] | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    """从 ``steps_detail`` 汇总整轮 ``(llm_elapsed, ttft, tool_elapsed, approval_elapsed)``。

    历史老数据没有拆分字段时返回 ``None``，绝不把「未知」伪装成 ``0.0``。
    """
    if not steps_detail:
        return None, None, None, None

    llm_sum = 0.0
    has_llm = False
    tool_sum = 0.0
    has_tool = False
    approval_sum = 0.0
    has_approval = False
    first_ttft: float | None = None

    for step in steps_detail:
        llm = step.get("llm_elapsed")
        if isinstance(llm, (int, float)):
            has_llm = True
            llm_sum += float(llm)
        ttft = step.get("ttft")
        if first_ttft is None and isinstance(ttft, (int, float)):
            first_ttft = float(ttft)
        tool = step.get("tool_elapsed")
        if isinstance(tool, (int, float)):
            has_tool = True
            tool_sum += float(tool)
        approval = step.get("approval_elapsed")
        if isinstance(approval, (int, float)):
            has_approval = True
            approval_sum += float(approval)

    return (
        round(llm_sum, 3) if has_llm else None,
        round(first_ttft, 3) if first_ttft is not None else None,
        round(tool_sum, 3) if has_tool else None,
        round(approval_sum, 3) if has_approval else None,
    )


__all__ = [
    "MAX_BUFFERED_EVENTS",
    "MAX_PAYLOAD_CHARS",
    "MemoryTraceStore",
    "TraceObserver",
    "TraceRecord",
    "TraceStore",
    "summarize_steps_timing",
]
