"""Channel discovery and lifecycle management."""

from __future__ import annotations

from collections.abc import Sequence

from doudou.channels.base import Channel
from doudou.channels.feishu import FeishuChannel
from doudou.config.loader import Settings
from doudou.gateway.router import MessageRouter
from doudou.observability.logging import get_logger

logger = get_logger(__name__)


def build_enabled_channels(settings: Settings, router: MessageRouter) -> list[Channel]:
    """按配置构造需要**常驻运行**的通道。

    CLI 通道是「用户敲命令时才存在」的，不由服务托管，因此不在这里；
    它同样实现了 ``EventSink``，需要时直接构造即可。
    """
    channels: list[Channel] = []

    feishu = settings.app.channels.feishu
    if feishu.enabled:
        if feishu.mode != "websocket":
            logger.warning(
                "channels.feishu_mode_unsupported",
                mode=feishu.mode,
                hint="目前只实现了 websocket 长连接模式，callback 模式仍会按长连接启动",
            )
        channels.append(FeishuChannel(settings, router))
    else:
        logger.debug("channels.feishu_disabled")

    return channels


async def start_channels(channels: Sequence[Channel]) -> list[Channel]:
    """逐个启动；单个通道失败只告警，不影响其它通道与 HTTP 服务。

    这是刻意的：飞书连不上不应该让整个服务起不来 —— 运维仍然需要
    ``/health`` 与 API 来排查问题。
    """
    started: list[Channel] = []
    for channel in channels:
        try:
            await channel.start()
        except Exception as exc:
            logger.error(
                "channels.start_failed",
                channel=channel.name,
                error=str(exc),
                exc_info=True,
            )
            continue
        started.append(channel)
    return started


async def stop_channels(channels: Sequence[Channel]) -> None:
    """逐个停止，忽略单个停止失败。"""
    for channel in reversed(list(channels)):
        try:
            await channel.stop()
        except Exception as exc:
            logger.warning("channels.stop_failed", channel=channel.name, error=str(exc))


__all__ = ["build_enabled_channels", "start_channels", "stop_channels"]
