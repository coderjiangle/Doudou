"""时间表达（``doudou.scheduler.triggers``）。

钉住三件事：间隔写法与下限、cron 用 apscheduler 算对不对、``at`` 过期要报错。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from doudou.scheduler.triggers import (
    MIN_INTERVAL_SECONDS,
    ScheduleError,
    describe,
    next_fire_time,
    parse_duration,
)


def test_parse_duration_units() -> None:
    assert parse_duration("90") == 90
    assert parse_duration("90s") == 90
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400


def test_parse_duration_rejects_bad_and_too_short() -> None:
    with pytest.raises(ScheduleError):
        parse_duration("很快")
    with pytest.raises(ScheduleError):
        parse_duration("5s")  # 低于下限
    assert MIN_INTERVAL_SECONDS >= 30


def test_next_fire_every_is_relative_to_now() -> None:
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    assert next_fire_time("every", "30m", None, now=now) == now + timedelta(minutes=30)


def test_next_fire_at_in_past_is_none() -> None:
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    assert next_fire_time("at", "2026-10-30T11:00:00", None, now=now) is None
    upcoming = next_fire_time("at", "2026-10-30T13:00:00", None, now=now)
    assert upcoming == datetime(2026, 10, 30, 13, 0, tzinfo=UTC)


def test_next_fire_cron_uses_timezone() -> None:
    # 每天 09:00（Asia/Shanghai）→ UTC 01:00
    now = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)

    upcoming = next_fire_time("cron", "0 9 * * *", "Asia/Shanghai", now=now)

    assert upcoming == datetime(2026, 10, 30, 1, 0, tzinfo=UTC)


def test_next_fire_cron_bad_expr_raises() -> None:
    with pytest.raises(ScheduleError):
        next_fire_time("cron", "not a cron", None, now=datetime.now(UTC))


def test_unknown_kind_raises() -> None:
    with pytest.raises(ScheduleError):
        next_fire_time("weekly", "x", None, now=datetime.now(UTC))  # type: ignore[arg-type]


def test_describe_humanizes_intervals() -> None:
    assert describe("every", "120") == "每 2 分钟"
    assert describe("every", "2h") == "每 2 小时"
    assert describe("at", "2026-11-01T09:00:00").startswith("一次")
    assert describe("cron", "0 9 * * *").startswith("cron")
