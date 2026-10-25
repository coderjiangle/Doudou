"""审计日志：谁、何时、对哪个工具、做了什么、结果如何、是否经人确认。

``approvals`` 记「确认这件事本身」（请求内容、状态、理由），``audit_events``
记「工具调用这件事」（参数摘要、结果摘要、耗时、是否经过确认）。两张表通过
``tool_call_id`` 对得上，所以「为什么批准」不会丢线索。

参数先过 ``security.secrets.redact``（``api_key`` / ``token`` 之类打码），
再按 ``max_arg_chars`` 截断 —— 一个 20 万字的写文件调用不该把审计表撑爆；
结果只留开头一段与长度，全文在历史消息与日志里都有。截断量单独记在
``args_truncated`` / ``result_chars`` 里，看记录的人才知道这里有缺口。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from doudou.agent.events import RunEvent
from doudou.observability.logging import get_logger
from doudou.observability.observers import BaseObserver, RunContext, RunSummary
from doudou.security.secrets import redact

logger = get_logger(__name__)

#: 审计事件的种类。
#:
#: ``tool_call`` 一条记录会经历两次写入（先建、后补结果），因此
#: ``status`` 才是它的终态；其余类型一次写完。
AuditKind = Literal["run", "tool", "error"]

#: 审计记录的终态。
AuditStatus = Literal["running", "ok", "failed", "rejected"]


@dataclass(slots=True)
class AuditEntry:
    """一条审计记录。"""

    #: 稳定主键：工具调用用 ``{thread}:{call_id}``，运行用 ``{thread}:{run_id}``。
    audit_id: str
    kind: AuditKind
    thread_id: str
    channel: str = ""
    conversation_id: str = ""
    user_id: str | None = None
    tool_name: str = ""
    tool_call_id: str = ""
    step: int = 0
    #: 参数摘要（已打码 + 截断），JSON 可序列化的字典。
    args: dict[str, Any] = field(default_factory=dict)
    #: 被截断掉的字符数；0 表示没截断。
    args_truncated: int = 0
    #: 结果摘要（已截断）。
    result_preview: str = ""
    #: 结果原文长度（截断前）。
    result_chars: int = 0
    status: AuditStatus = "running"
    #: 是否经过人工确认：``None`` = 该工具不需要确认（没问过）。
    approved: bool | None = None
    elapsed: float | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """构造时就打码参数 —— 把「审计表不含明文密钥」做成**不可能违反**的规则。

        :func:`~doudou.observability.audit.summarize_args` 已经打过一次码
        （幂等），这里再兜一道的原因：``AuditEntry`` 是公开数据结构，
        将来某个新调用点忘了走 ``summarize_args`` 时，也不该把密钥抄进库。
        审计的价值是事后还原，不是留存凭据。
        """
        safe = redact(self.args)
        if isinstance(safe, dict):
            self.args = safe

    def as_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "kind": self.kind,
            "thread_id": self.thread_id,
            "channel": self.channel,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "step": self.step,
            "args": dict(self.args),
            "args_truncated": self.args_truncated,
            "result_preview": self.result_preview,
            "result_chars": self.result_chars,
            "status": self.status,
            "approved": self.approved,
            "elapsed": self.elapsed,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class AuditStore(Protocol):
    """审计记录的落点。``memory`` 实现用于无数据库的部署与测试。"""

    async def append(self, entry: AuditEntry) -> None:
        """写入或更新（同一个 ``audit_id`` 只应留下一行）。"""
        ...

    async def append_many(self, entries: list[AuditEntry]) -> None:
        """批量写入（一次提交）。见 ``buffering.py``。"""
        ...

    async def list_recent(
        self, *, limit: int = 100, thread_id: str | None = None
    ) -> list[AuditEntry]: ...

    async def get(self, audit_id: str) -> AuditEntry | None: ...


class MemoryAuditStore:
    """进程内审计（有容量上限的环形缓冲）。

    上限是必须的：审计是**每次工具调用**都写的东西，不设上限的进程内列表
    就是一条缓慢的内存泄漏。超出后丢最旧的 —— 排障看的总是最近的。
    """

    def __init__(self, *, max_entries: int = 2000) -> None:
        self.max_entries = max(1, max_entries)
        self._entries: dict[str, AuditEntry] = {}

    async def append(self, entry: AuditEntry) -> None:
        self._entries.pop(entry.audit_id, None)  # 重新插入以维持「最近在最前」
        self._entries[entry.audit_id] = entry
        while len(self._entries) > self.max_entries:
            oldest = next(iter(self._entries))
            self._entries.pop(oldest, None)

    async def append_many(self, entries: list[AuditEntry]) -> None:
        for entry in entries:
            await self.append(entry)

    async def list_recent(
        self, *, limit: int = 100, thread_id: str | None = None
    ) -> list[AuditEntry]:
        items = [item for item in self._entries.values() if _matches(item, thread_id)]
        return list(reversed(items))[: max(1, limit)]

    async def get(self, audit_id: str) -> AuditEntry | None:
        return self._entries.get(audit_id)


def _matches(entry: AuditEntry, thread_id: str | None) -> bool:
    return thread_id is None or entry.thread_id == thread_id


class AuditObserver(BaseObserver):
    """把工具调用与运行结果写进审计表。"""

    name = "audit"

    def __init__(
        self,
        store: AuditStore,
        *,
        max_arg_chars: int = 2000,
        max_result_chars: int = 500,
    ) -> None:
        self.store = store
        self.max_arg_chars = max(0, max_arg_chars)
        self.max_result_chars = max(0, max_result_chars)
        self._runs: dict[str, AuditEntry] = {}
        self._tools: dict[str, AuditEntry] = {}

    # ---------------------------------------------------------------- 回调

    async def on_run_start(self, context: RunContext) -> None:
        entry = AuditEntry(
            audit_id=f"{context.thread_id}:run",
            kind="run",
            thread_id=context.thread_id,
            channel=context.channel,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
            args={"input_chars": len(context.text)},
            status="running",
        )
        self._runs[context.thread_id] = entry
        await self.store.append(entry)

    async def on_event(self, context: RunContext, event: RunEvent) -> None:
        if event.type == "tool_call":
            await self._on_tool_call(context, event)
        elif event.type == "approval":
            self._on_approval(context, event)
        elif event.type == "tool_result":
            await self._on_tool_result(context, event)

    async def on_run_end(self, context: RunContext, summary: RunSummary) -> None:
        entry = self._runs.pop(context.thread_id, None)
        if entry is None:
            return
        entry.status = "ok" if summary.ok else "failed"
        entry.step = summary.steps
        entry.elapsed = summary.elapsed
        entry.error = summary.error
        entry.args = {
            **entry.args,
            "tool_calls": summary.tool_calls,
            "approvals": summary.approvals,
            "rejections": summary.rejections,
            "input_tokens": summary.usage.get("input_tokens", 0),
            "output_tokens": summary.usage.get("output_tokens", 0),
        }
        entry.updated_at = datetime.now(UTC)
        await self.store.append(entry)

    async def aclose(self) -> None:
        self._runs.clear()
        self._tools.clear()

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _tool_key(context: RunContext, event: RunEvent) -> str:
        return f"{context.thread_id}:tool:{event.tool_call_id or event.step}"

    async def _on_tool_call(self, context: RunContext, event: RunEvent) -> None:
        args, truncated = summarize_args(event.tool_args or {}, self.max_arg_chars)
        entry = AuditEntry(
            audit_id=self._tool_key(context, event),
            kind="tool",
            thread_id=context.thread_id,
            channel=context.channel,
            conversation_id=context.conversation_id,
            user_id=context.user_id,
            tool_name=event.tool_name or "",
            tool_call_id=event.tool_call_id or "",
            step=event.step,
            args=args,
            args_truncated=truncated,
            status="running",
        )
        self._tools[entry.audit_id] = entry
        await self.store.append(entry)

    def _on_approval(self, context: RunContext, event: RunEvent) -> None:
        """只更新内存里的那条，等 ``tool_result`` 一起落库。

        分开写会多一次数据库往返，而「确认结果」本身不影响任何查询 ——
        它会随下一次写入一起落盘。
        """
        entry = self._tools.get(self._tool_key(context, event))
        if entry is None:
            return
        entry.approved = event.tool_ok
        if event.text:
            entry.error = None if event.tool_ok else event.text

    async def _on_tool_result(self, context: RunContext, event: RunEvent) -> None:
        entry = self._tools.pop(self._tool_key(context, event), None)
        if entry is None:
            # 没看到 tool_call（例如事件流被截断）也要留痕，否则审计有空洞。
            entry = AuditEntry(
                audit_id=self._tool_key(context, event),
                kind="tool",
                thread_id=context.thread_id,
                channel=context.channel,
                conversation_id=context.conversation_id,
                user_id=context.user_id,
                tool_name=event.tool_name or "",
                tool_call_id=event.tool_call_id or "",
                step=event.step,
            )
        result = event.result or ""
        entry.result_chars = len(result)
        entry.result_preview = _preview(result, self.max_result_chars)
        if entry.approved is False:
            entry.status = "rejected"
        else:
            entry.status = "ok" if event.tool_ok else "failed"
        entry.elapsed = event.tool_elapsed
        if not event.tool_ok:
            entry.error = _preview(result, 300)
        entry.updated_at = datetime.now(UTC)
        await self.store.append(entry)


def summarize_args(args: dict[str, Any], limit: int) -> tuple[dict[str, Any], int]:
    """打码 + 截断工具参数，返回 ``(摘要, 被截断的字符数)``。

    先打码再截断（顺序反了会把打码结果切坏），并且**逐字段**截断 ——
    整体截断 JSON 会让结果不可解析，审计记录也就查不动了。
    """
    safe = redact(args)
    if not isinstance(safe, dict):  # pragma: no cover - redact 对 dict 一定返回 dict
        return {"value": str(safe)[:limit]}, 0

    summarized: dict[str, Any] = {}
    truncated = 0
    for key, value in safe.items():
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        )
        if not isinstance(text, str):
            text = str(text)
        if len(text) > limit:
            truncated += len(text) - limit
            summarized[key] = text[:limit] + f"…（截断 {len(text) - limit} 字）"
        else:
            summarized[key] = text if isinstance(value, str) else value
    return summarized, truncated


def _preview(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（共 {len(text)} 字）"


__all__ = [
    "AuditEntry",
    "AuditKind",
    "AuditObserver",
    "AuditStatus",
    "AuditStore",
    "MemoryAuditStore",
    "summarize_args",
]
