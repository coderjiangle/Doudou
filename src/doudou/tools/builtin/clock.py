"""Current time tool with timezone support."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings
from doudou.tools.decorators import annotate

_WEEKDAYS = "一二三四五六日"

GROUP = "clock"


def resolve_timezone(name: str | None) -> ZoneInfo:
    """把 IANA 时区名解析成 :class:`ZoneInfo`。

    解析失败时抛出带建议的 ``ValueError``，让模型能据此纠正参数。
    """
    candidate = (name or "").strip() or "UTC"
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"无法识别的时区 {candidate!r}；请使用 IANA 名称，例如 Asia/Shanghai、UTC。"
        ) from exc


def build_tools(settings: Settings) -> Sequence[BaseTool]:
    """构造时间工具，默认时区取自 ``runtime.timezone``。"""
    default_timezone = settings.app.runtime.timezone

    @tool
    def now(timezone: str = "") -> str:
        """获取当前的日期与时间。

        Args:
            timezone: IANA 时区名（例如 "Asia/Shanghai"、"UTC"）。
                留空表示使用默认时区。

        Returns:
            形如 "2026-10-23 14:05:31 CST（周五）" 的字符串。

        需要回答「现在几点」「今天几号」「今天星期几」这类问题时使用本工具，
        不要凭训练数据里的时间推测。
        """
        zone = resolve_timezone(timezone or default_timezone)
        current = datetime.now(zone)
        weekday = _WEEKDAYS[current.weekday()]
        abbreviation = current.tzname() or ""
        return f"{current:%Y-%m-%d %H:%M:%S} {abbreviation}（周{weekday}）".strip()

    # 把实际生效的默认时区补进描述，让模型知道留空时用哪个时区。
    # 只能事后追加：docstring 里做字符串插值会让它不再是合法的 __doc__。
    now.description = f"{now.description}\n\n当前默认时区：{default_timezone}。"

    # 纯读操作：显式声明，否则会被当成有副作用而要求人工确认。
    annotate(now, group=GROUP, side_effects=False)
    return [now]


__all__ = ["GROUP", "build_tools", "resolve_timezone"]
