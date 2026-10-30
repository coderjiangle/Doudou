"""投递目标：创建时定目标，触发时解析目标。

规则（对应需求）：

* 在**飞书群**里建的任务 → 推给那个群；
* 在**飞书私聊**里建的任务 → 推给那条私聊；
* 在 **CLI / Web** 里建的任务 → 推给「飞书的私聊」，但**创建时不知道是哪条**，
  因此触发时再解析（用户私聊过机器人就有绑定，否则没有可推送目标 → 跳过本次）。

任务本身**不绑定任何会话**，所以这里只处理「渠道 + 会话」这一对投递坐标。
"""

from __future__ import annotations

from doudou.channels.base import InboundMessage
from doudou.observability.logging import get_logger
from doudou.scheduler.store import SchedulerStore
from doudou.scheduler.types import ScheduledTask

logger = get_logger(__name__)

#: CLI / Web 任务的默认推送渠道。个人助手的「私聊」就是飞书。
PUSH_CHANNEL = "feishu"
#: 飞书的私聊对话类型。
P2P_CHAT_TYPES = ("p2p",)


def creation_target(inbound: InboundMessage) -> tuple[str, str | None]:
    """按创建者所在会话决定投递坐标 ``(channel, conversation_id)``。

    飞书（群或私聊）直接用当前会话；其它渠道（CLI / Web）没有渠道会话，
    返回 ``(feishu, None)`` —— 触发时再解析成飞书私聊。
    """
    if inbound.channel == PUSH_CHANNEL:
        return PUSH_CHANNEL, inbound.conversation_id
    return PUSH_CHANNEL, None


def is_private_chat(inbound: InboundMessage) -> bool:
    """是否是飞书私聊（用于决定要不要记「用户 → 私聊会话」绑定）。"""
    chat_type = str(inbound.raw.get("chat_type") or "p2p")
    return chat_type in P2P_CHAT_TYPES


class TargetRecorder:
    """飞书私聊到达时记下「用户 → 那条私聊会话」的映射。

    挂到 ``MessageRouter.on_inbound``：CLI / Web 建的任务将来就靠它找到投递目标。
    """

    def __init__(self, store: SchedulerStore) -> None:
        self.store = store

    async def record(self, inbound: InboundMessage) -> None:
        if inbound.channel != PUSH_CHANNEL or not inbound.user_id:
            return
        if not is_private_chat(inbound):
            return
        await self.store.remember_target(
            channel=inbound.channel,
            user_id=inbound.user_id,
            conversation_id=inbound.conversation_id,
        )
        logger.debug(
            "scheduler.target_recorded",
            channel=inbound.channel,
            user_id=inbound.user_id,
            conversation_id=inbound.conversation_id,
        )


async def resolve_delivery(
    store: SchedulerStore, task: ScheduledTask, *, owner_user_id: str | None
) -> tuple[str, str] | None:
    """解析一次触发该推到哪；解析不到返回 ``None``（调用方跳过本次）。"""
    if task.conversation_id:
        return task.channel, task.conversation_id
    resolved = await store.resolve_target(channel=task.channel, owner_user_id=owner_user_id)
    if not resolved:
        logger.info(
            "scheduler.target_unresolved",
            task_id=task.task_id,
            channel=task.channel,
            hint="没有可推送的私聊目标：让用户先私聊机器人，或配置 scheduler.owner_user_id",
        )
        return None
    return task.channel, resolved


__all__ = [
    "P2P_CHAT_TYPES",
    "PUSH_CHANNEL",
    "TargetRecorder",
    "creation_target",
    "is_private_chat",
    "resolve_delivery",
]
