"""定时任务工具：在聊天里创建 / 查看 / 取消定时任务。

**任务不绑定会话**：创建时只记「投递目标」——
飞书群 → 那个群，飞书私聊 → 那条私聊，CLI/Web → 飞书私聊（触发时再解析）。
到点后由 ``doudou/scheduler/runtime`` 过一遍 LLM，把结果推到目标会话。

依赖不满足就不注册：没有调度存储（如无库的临时 CLI 会话）时这一组工具不出现。
工具写成 ``async``：ReAct 循环用 ``tool.ainvoke`` 调用，异步存储可直接 await。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from langchain_core.tools import BaseTool, tool

from doudou.channels.base import current_inbound
from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.scheduler.store import SchedulerStore
from doudou.scheduler.targets import PUSH_CHANNEL, creation_target
from doudou.scheduler.triggers import (
    ScheduleError,
    describe,
    next_fire_time,
    resolve_zone,
)
from doudou.scheduler.types import ScheduledTask, TaskKind
from doudou.tools.decorators import annotate

logger = get_logger(__name__)

GROUP = "schedule"

_KINDS = ("at", "every", "cron")


def build_tools(
    settings: Settings,
    *,
    store: SchedulerStore | None = None,
    owner_user_id: str | None = None,
    default_timezone: str = "UTC",
) -> Sequence[BaseTool]:
    """构造定时任务工具。

    Args:
        settings: 当前配置。
        store: 调度存储；为 ``None``（如无库的临时 CLI）时不注册任何工具。
        owner_user_id: 覆盖 ``scheduler.owner_user_id``。
        default_timezone: 解释 cron/间隔用的默认时区。
    """
    if store is None:
        return []

    config = settings.app.scheduler
    zone = config.timezone or default_timezone

    def _target() -> tuple[str, str | None, str | None]:
        """返回 ``(channel, conversation_id, created_by)``。"""
        inbound = current_inbound()
        if inbound is None:
            # 没有渠道上下文（例如极少数直接调用）：退回「飞书私聊，触发时解析」。
            return PUSH_CHANNEL, None, None
        channel, conversation_id = creation_target(inbound)
        return channel, conversation_id, inbound.user_id

    @tool
    async def schedule_create(
        name: str, prompt: str, kind: str, expr: str, timezone: str = ""
    ) -> str:
        """创建一个定时任务：到点后按 prompt 跑一遍，把结果推到当前会话。

        Args:
            name: 任务名（简短，用于列表与触发消息的前缀）。
            prompt: 到点要执行的指令（作为一条消息进入会话，交给模型处理）。
            kind: 触发方式，取 ``at`` / ``every`` / ``cron`` 之一。
            expr: 时间表达式——
                ``at`` → ISO 时间，如 ``2026-11-01T09:00:00``；
                ``every`` → 间隔，如 ``30m`` / ``2h`` / ``1d``；
                ``cron`` → 5 段表达式，如 ``0 9 * * *``（每天 9 点）。
            timezone: IANA 时区名（如 ``Asia/Shanghai``）；留空用默认时区。

        Returns:
            创建结果（含任务 id 与下次触发时间）；失败时说明原因且不改任何状态。

        用户说「每天提醒我…」「半小时后提醒我…」这类**带时间**的诉求时使用。
        投递目标由当前会话决定：群里建的任务推给那个群，私聊 / CLI / Web 建的任务推给飞书私聊。
        """
        kind_value = (kind or "").strip().lower()
        if kind_value not in _KINDS:
            return f"kind 只能是 {' / '.join(_KINDS)}，收到的是 {kind!r}。"
        kind_typed = cast("TaskKind", kind_value)
        if not name.strip():
            return "任务名不能为空。"
        if not prompt.strip():
            return "prompt 不能为空。"

        if config.max_tasks and await store.count() >= config.max_tasks:
            return f"任务数已达上限（{config.max_tasks}），请先取消一些再建。"

        now = datetime.now(UTC)
        try:
            upcoming = next_fire_time(
                kind_typed,
                expr,
                timezone.strip() or None,
                now=now,
                default_timezone=zone,
            )
        except ScheduleError as exc:
            return f"时间表达式无法解析：{exc}"
        if upcoming is None:
            return f"{describe(kind_typed, expr)} 对应的时间已经过去，请给一个将来的时间。"
        channel, conversation_id, created_by = _target()
        task = ScheduledTask(
            task_id=f"task-{uuid4().hex[:12]}",
            name=name.strip(),
            prompt=prompt.strip(),
            kind=kind_typed,
            expr=expr.strip(),
            timezone=timezone.strip() or None,
            channel=channel,
            conversation_id=conversation_id,
            created_by=created_by,
            enabled=True,
            next_run_at=upcoming,
        )
        await store.create(task)
        logger.info(
            "scheduler.task_created",
            task_id=task.task_id,
            kind=task.kind,
            channel=task.channel,
            conversation_id=task.conversation_id,
            next_run_at=task.next_run_at.isoformat(),
        )
        return "\n".join(
            [
                f"已创建定时任务 {task.task_id}",
                f"名称：{task.name}",
                f"触发：{describe(task.kind, task.expr)}",
                f"下次触发：{_local(task.next_run_at, zone)}",
                f"投递目标：{task.target_label}",
            ]
        )

    @tool
    async def schedule_list() -> str:
        """列出所有定时任务及其状态（只读，不会改动任何任务）。

        Returns:
            每个任务的 id、名称、触发规则、下次触发时间、投递目标与上次结果。

        用户问「有哪些定时任务」「它什么时候跑」这类问题时使用。
        """
        tasks = await store.list_all()
        if not tasks:
            return "当前没有任何定时任务。"
        lines = [f"共 {len(tasks)} 个定时任务："]
        for task in tasks:
            flag = "" if task.enabled else "（已停用）"
            tail = f"｜上次 {task.last_status}" if task.last_status else ""
            lines.append(
                f"- {task.task_id}｜{task.name}{flag}｜{describe(task.kind, task.expr)}"
                f"｜下次 {_local(task.next_run_at, zone)}｜目标 {task.target_label}"
                f"｜已跑 {task.run_count} 次{tail}"
            )
        return "\n".join(lines)

    @tool
    async def schedule_cancel(task_id: str) -> str:
        """取消（删除）一个定时任务。

        Args:
            task_id: ``schedule_list`` 里看到的任务 id（如 ``task-1a2b3c``）。

        Returns:
            取消结果；任务不存在时如实说明，不报错。

        用户说「取消 / 删掉那个定时任务」时使用；建议先用 ``schedule_list`` 确认 id。
        """
        if await store.delete(task_id.strip()):
            logger.info("scheduler.task_cancelled", task_id=task_id)
            return f"已取消定时任务 {task_id}。"
        return f"没有找到任务 {task_id}，用 schedule_list 看一下现有任务。"

    annotate(schedule_create, group=GROUP, side_effects=True)
    annotate(schedule_cancel, group=GROUP, side_effects=True)
    annotate(schedule_list, group=GROUP, side_effects=False)
    return [schedule_create, schedule_list, schedule_cancel]


def _local(moment: datetime, zone: str) -> str:
    return moment.astimezone(resolve_zone(zone)).strftime("%Y-%m-%d %H:%M %Z")


__all__ = ["GROUP", "build_tools"]
