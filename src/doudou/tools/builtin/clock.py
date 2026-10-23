"""Time and date tools with timezone support."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings

_WEEKDAYS = "一二三四五六日"


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
    """构造时间类工具，默认时区取自 ``runtime.timezone``。"""
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

    @tool
    def days_between(start_date: str, end_date: str) -> str:
        """计算两个日期之间相差多少天。

        Args:
            start_date: 起始日期，ISO 格式 ``YYYY-MM-DD``。
            end_date: 结束日期，ISO 格式 ``YYYY-MM-DD``。

        Returns:
            形如 "2026-10-01 到 2026-10-23 相差 22 天" 的字符串；
            ``end_date`` 早于 ``start_date`` 时天数为负数。
        """
        try:
            start = date.fromisoformat(start_date.strip())
            end = date.fromisoformat(end_date.strip())
        except ValueError as exc:
            raise ValueError(
                f"日期格式错误，需要 YYYY-MM-DD，收到 {start_date!r} 与 {end_date!r}。"
            ) from exc
        delta = (end - start).days
        return f"{start.isoformat()} 到 {end.isoformat()} 相差 {delta} 天"

    # 把实际生效的默认时区补进描述，让模型知道留空时用哪个时区。
    # 只能事后追加：docstring 里做字符串插值会让它不再是合法的 __doc__。
    now.description = f"{now.description}\n\n当前默认时区：{default_timezone}。"

    return [now, days_between]


__all__ = ["build_tools", "resolve_timezone"]
