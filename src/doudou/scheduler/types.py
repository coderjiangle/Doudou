"""定时任务的领域对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

#: 时间表达种类。
#:
#: - ``at``    —— 一次性，``expr`` 是 ISO 时间；
#: - ``every`` —— 固定间隔，``expr`` 是时长（秒数或 ``30m`` / ``2h`` / ``1d``）；
#: - ``cron``  —— 5 段 crontab，``expr`` 形如 ``0 9 * * *``。
TaskKind = Literal["at", "every", "cron"]

#: 一次触发的结果：``ok`` / ``failed`` / ``skipped``。
#:
#: ``skipped`` 是**没有可推送目标**（飞书私聊没绑定、目标通道没起来）时的结论 ——
#: 跳过整次触发，不跑模型也不推送。
TaskStatus = Literal["ok", "failed", "skipped"]


@dataclass(slots=True)
class ScheduledTask:
    """一条定时任务。"""

    task_id: str
    name: str
    #: 触发时交给模型的指令。
    prompt: str
    kind: TaskKind
    expr: str
    #: 解释 cron/间隔用的时区名（IANA）；``None`` 表示用运行期默认时区。
    timezone: str | None
    #: 投递目标渠道（如 ``feishu``）。
    channel: str
    #: 投递目标会话；``None`` 表示触发时再解析（CLI/Web 建的任务）。
    conversation_id: str | None = None
    created_by: str | None = None
    enabled: bool = True
    next_run_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    run_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def target_label(self) -> str:
        """给人看的投递目标描述。"""
        if self.conversation_id:
            return f"{self.channel}:{self.conversation_id}"
        return f"{self.channel}:（自动解析私聊）"

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "prompt": self.prompt,
            "kind": self.kind,
            "expr": self.expr,
            "timezone": self.timezone,
            "channel": self.channel,
            "conversation_id": self.conversation_id,
            "created_by": self.created_by,
            "enabled": self.enabled,
            "next_run_at": self.next_run_at.isoformat(),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "run_count": self.run_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


__all__ = ["ScheduledTask", "TaskKind", "TaskStatus"]
