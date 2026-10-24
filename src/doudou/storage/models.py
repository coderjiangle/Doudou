"""ORM models: threads, messages, channel bindings, approvals, audit, usage.

六张表的分工：

* ``threads``          —— 一个会话（= 一个 agent thread），承载渠道归属与时间戳；
* ``messages``         —— 线程内的消息，``seq`` 保证顺序，工具调用字段以 JSON 存；
* ``session_bindings`` —— 「渠道会话 → thread」的映射，让会话归属在重启后仍然成立；
* ``approvals``        —— 人工确认的请求与决策（跨重启可查）；
* ``audit_events``     —— 每次工具调用与每次运行的审计留痕；
* ``usage_daily``      —— 按「天 × 会话」累计的 token 与费用。

时间戳统一存 UTC。注意 SQLite 不保留时区信息，读出来的可能是 naive datetime，
因此对外输出前一律走 :func:`as_utc` 补上 UTC。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """把可能为 naive 的时间戳补成 UTC。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


class ThreadModel(Base):
    """一个 agent 会话。"""

    __tablename__ = "threads"

    thread_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    #: 来源渠道（cli / api / feishu ...）
    channel: Mapped[str | None] = mapped_column(String(32), index=True)
    #: 渠道内的会话标识（飞书 chat_id 等）
    conversation_id: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    messages: Mapped[list[MessageModel]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="MessageModel.seq",
        lazy="selectin",
    )

    __table_args__ = (Index("ix_threads_channel_conversation", "channel", "conversation_id"),)


class MessageModel(Base):
    """线程内的一条消息。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(
        String(120), ForeignKey("threads.thread_id", ondelete="CASCADE"), index=True
    )
    #: 线程内单调递增序号，用于稳定排序与裁剪
    seq: Mapped[int] = mapped_column(Integer)
    #: human | ai | tool | system
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    #: LangChain 的 tool_calls（仅 ai 角色可能非空）
    tool_calls: Mapped[Any] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(160))
    name: Mapped[str | None] = mapped_column(String(120))
    #: tool 角色专用：success | error
    status: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    thread: Mapped[ThreadModel] = relationship(back_populates="messages")

    __table_args__ = (UniqueConstraint("thread_id", "seq", name="uq_messages_thread_seq"),)


class SessionBindingModel(Base):
    """「渠道会话 → thread」的持久化映射，保证重启后会话归属不变。"""

    __tablename__ = "session_bindings"

    channel: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ApprovalModel(Base):
    """人工确认的请求与决策。

    为什么要落库：确认是**有后果的事实**（「谁在什么时候同意了对哪个文件做什么」）。
    进程重启后必须还能查得到 —— 与「待决的运行会不会续跑」是两件事，
    后者做不到，前者必须做到。
    """

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 模型给的 tool_call_id；用它在同一个调用上复用决策，避免重复问。
    call_id: Mapped[str | None] = mapped_column(String(160), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(120), index=True)
    tool_name: Mapped[str] = mapped_column(String(120))
    args: Mapped[Any] = mapped_column(JSON, nullable=True)
    step: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditModel(Base):
    """审计：每次工具调用（以及每次运行）一条记录。

    与 ``approvals`` 的分工：那张表记「确认请求与理由」，这张表记
    「工具调用与它的结果」。两者用 ``tool_call_id`` 对得上。

    一条工具记录会写两次（先建、后补结果），因此 ``audit_id`` 是主键、
    用 upsert 写入 —— 分开写会造出两行，审计就变成噪声了。
    """

    __tablename__ = "audit_events"

    audit_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    thread_id: Mapped[str] = mapped_column(String(120), index=True)
    channel: Mapped[str | None] = mapped_column(String(32))
    conversation_id: Mapped[str | None] = mapped_column(String(200))
    user_id: Mapped[str | None] = mapped_column(String(120))
    tool_name: Mapped[str | None] = mapped_column(String(120), index=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(160), index=True)
    step: Mapped[int] = mapped_column(Integer, default=0)
    #: 参数摘要（已打码 + 截断）。
    args: Mapped[Any] = mapped_column(JSON, nullable=True)
    args_truncated: Mapped[int] = mapped_column(Integer, default=0)
    result_preview: Mapped[str | None] = mapped_column(Text)
    result_chars: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), index=True)
    #: 是否经过人工确认：``NULL`` = 该工具不需要确认（根本没问过）。
    approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    elapsed: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageModel(Base):
    """按「天 × 会话」累计的 token 与费用。

    粒度选 (day, thread_id) 而不是每次运行一行：用量查询几乎总是
    「某条会话花了多少」「今天花了多少」，逐运行记录会让表无界增长，
    而聚合结果并不需要那么细。真要逐次对账，日志里每次运行都有 token 数。
    """

    __tablename__ = "usage_daily"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(120))
    runs: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    approvals: Mapped[int] = mapped_column(Integer, default=0)
    rejections: Mapped[int] = mapped_column(Integer, default=0)
    #: 累计费用。``NULL`` = 有运行的价格查不到，**不能当 0**。
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


__all__ = [
    "ApprovalModel",
    "AuditModel",
    "Base",
    "MessageModel",
    "SessionBindingModel",
    "ThreadModel",
    "UsageModel",
    "as_utc",
    "utcnow",
]
