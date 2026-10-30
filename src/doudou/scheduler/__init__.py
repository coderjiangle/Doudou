"""定时任务：类型、时间计算、存储与运行。

分四块，各自单一职责：

* :mod:`.types`    —— 领域对象（:class:`ScheduledTask`）。
* :mod:`.triggers` —— 时间表达（``at`` / ``every`` / ``cron``）→ 下一个触发点。
* :mod:`.store`    —— 落点（协议 + 内存 + 传 SQL）。
* :mod:`.runtime`  —— 轮询到期任务，交给 ``MessageRouter`` 跑一轮并推送。

**任务不绑定任何会话**：只记投递目标（``channel`` + ``conversation_id``）。触发时
在目标会话里解析出 thread，于是那一轮的消息并入该会话上下文。
"""

from __future__ import annotations

from doudou.scheduler.types import ScheduledTask, TaskKind, TaskStatus

__all__ = ["ScheduledTask", "TaskKind", "TaskStatus"]
