"""时间表达 → 下一个触发点。

三种写法：

* ``at``    —— ISO 时间，一次性；
* ``every`` —— 固定间隔（``90`` / ``30s`` / ``5m`` / ``2h`` / ``1d``）；
* ``cron``  —— 5 段 crontab，用 ``apscheduler`` 的 ``CronTrigger`` 解析
  （cron 语法坑很多，自己写解析器不值得；这里只借它算时间，不用它的调度器 ——
  任务真相源在 DB，双份状态只会带来重启重同步的麻烦）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from doudou.observability.logging import get_logger
from doudou.scheduler.types import TaskKind

logger = get_logger(__name__)

#: 时长写法：``90``（秒）、``90s``、``5m``、``2h``、``1d``。
_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}

#: 间隔下限：比轮询间隔还密的间隔没有意义，只会把模型打爆。
MIN_INTERVAL_SECONDS = 30


class ScheduleError(ValueError):
    """时间表达式非法。"""


def parse_duration(text: str) -> int:
    """把 ``30m`` / ``2h`` / ``90`` 之类的写法转成秒数。"""
    match = _DURATION.match(text or "")
    if match is None:
        raise ScheduleError(f"无法识别的间隔 {text!r}：请用 90 / 90s / 5m / 2h / 1d")
    amount = int(match.group(1))
    seconds = amount * _UNIT_SECONDS[match.group(2).lower()]
    if seconds < MIN_INTERVAL_SECONDS:
        raise ScheduleError(f"间隔太短（{seconds}s）：最小 {MIN_INTERVAL_SECONDS}s")
    return seconds


def resolve_zone(name: str | None, *, default: str = "UTC") -> ZoneInfo:
    """解析 IANA 时区名；名字写错时回落并告警，不让任务建不出来。"""
    for candidate in (name, default):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("scheduler.unknown_timezone", timezone=candidate, fallback=default)
    return ZoneInfo("UTC")


def parse_at(expr: str, zone: ZoneInfo) -> datetime:
    """解析 ``at`` 的 ISO 时间；无时区时按给定时区解释。"""
    text = (expr or "").strip()
    if not text:
        raise ScheduleError("at 需要一个 ISO 时间，例如 2026-11-01T09:00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScheduleError(f"无法解析时间 {expr!r}：请用 ISO 格式，例如 2026-11-01T09:00") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(UTC)


def next_fire_time(
    kind: TaskKind, expr: str, timezone: str | None, *, now: datetime, default_timezone: str = "UTC"
) -> datetime | None:
    """算 ``now`` 之后的第一个触发点；``at`` 若已过期返回 ``None``。"""
    zone = resolve_zone(timezone, default=default_timezone)
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)

    if kind == "every":
        return moment + timedelta(seconds=parse_duration(expr))

    if kind == "at":
        target = parse_at(expr, zone)
        return target if target > moment else None

    if kind == "cron":
        return _next_cron(expr, zone, moment)

    raise ScheduleError(f"不支持的 kind={kind!r}（应为 at / every / cron）")


def _next_cron(expr: str, zone: ZoneInfo, now: datetime) -> datetime | None:
    from apscheduler.triggers.cron import CronTrigger

    try:
        trigger = CronTrigger.from_crontab(expr, timezone=zone)
    except ValueError as exc:
        raise ScheduleError(f"无法解析 cron 表达式 {expr!r}：{exc}") from exc
    upcoming = trigger.get_next_fire_time(None, now)
    if not isinstance(upcoming, datetime):
        return None
    return upcoming.astimezone(UTC)


def describe(kind: TaskKind, expr: str) -> str:
    """给工具输出用的一句话描述。"""
    if kind == "every":
        try:
            seconds = parse_duration(expr)
        except ScheduleError:
            return f"每 {expr}"
        if seconds % 86400 == 0:
            return f"每 {seconds // 86400} 天"
        if seconds % 3600 == 0:
            return f"每 {seconds // 3600} 小时"
        if seconds % 60 == 0:
            return f"每 {seconds // 60} 分钟"
        return f"每 {seconds} 秒"
    if kind == "at":
        return f"一次：{expr}"
    return f"cron：{expr}"


__all__ = [
    "MIN_INTERVAL_SECONDS",
    "ScheduleError",
    "describe",
    "next_fire_time",
    "parse_at",
    "parse_duration",
    "resolve_zone",
]
