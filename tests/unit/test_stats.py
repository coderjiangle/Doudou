"""统计聚合（``api.stats``）。

钉住的是统计页最容易出错的三件事：

1. 「未知费用」不能当 0（``cost`` 为 ``None`` 要原样透出）；
2. 「免确认」（``approved is None``）必须和「同意」分开 —— 否则会报告
   「全都经过人确认」，那是假的；
3. 趋势图要**补零**，不能只画有数据的那几天（否则折线会骗人）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from doudou.api.stats import RunSample, ToolSample, build_stats


def _run(*, days_ago: int = 0, ok: bool = True, elapsed: float = 1.0, **extra: object) -> RunSample:
    started = datetime.now(UTC) - timedelta(days=days_ago)
    base: dict[str, object] = {
        "started_at": started,
        "ok": ok,
        "elapsed": elapsed,
        "tool_calls": 1,
        "approvals": 0,
        "rejections": 0,
        "input_tokens": 10,
        "output_tokens": 5,
        "channel": "cli",
        "thread_id": "t1",
    }
    base.update(extra)
    return RunSample(**base)  # type: ignore[arg-type]


def test_empty_window_is_all_zeros() -> None:
    report = build_stats(runs=[], tools=[], usage_by_day={}, days=7)

    assert report["overview"]["runs"] == 0
    assert report["overview"]["success_rate"] is None  # 没有样本 → 不是 100%
    assert report["approvals"]["required"] == 0
    assert report["approvals"]["approval_rate"] is None
    # 补零：窗口内每一天都在，趋势图不会缺天
    assert len(report["by_day"]) == 7
    assert {day["runs"] for day in report["by_day"]} == {0}


def test_overview_counts_and_success_rate() -> None:
    runs = [_run(ok=True), _run(ok=True), _run(ok=False)]

    report = build_stats(runs=runs, tools=[], usage_by_day={}, days=7)

    overview = report["overview"]
    assert overview["runs"] == 3
    assert overview["ok_runs"] == 2
    assert overview["failed_runs"] == 1
    assert overview["success_rate"] == round(2 / 3, 4)
    assert overview["total_tokens"] == 45


def test_approvals_distinguish_auto_from_approved() -> None:
    tools = [
        ToolSample("now", "ok", None, 0.01, datetime.now(UTC)),  # 不需要确认
        ToolSample("workspace_write", "ok", True, 0.02, datetime.now(UTC)),  # 人同意了
        ToolSample("shell_exec", "rejected", False, None, datetime.now(UTC)),  # 人拒绝了
    ]

    report = build_stats(runs=[], tools=tools, usage_by_day={}, days=7)

    approvals = report["approvals"]
    assert approvals["required"] == 2  # 只有问过的才计入
    assert approvals["approvals"] == 1
    assert approvals["rejections"] == 1
    assert approvals["auto"] == 1
    assert approvals["approval_rate"] == 0.5

    by_name = {tool["name"]: tool for tool in report["tools"]}
    assert by_name["shell_exec"]["rejected"] == 1
    assert by_name["now"]["failed"] == 0


def test_unknown_cost_is_preserved_in_trend() -> None:
    today = datetime.now(UTC).date().isoformat()
    runs = [_run(ok=True)]

    report = build_stats(runs=runs, tools=[], usage_by_day={today: (10, 5, None)}, days=7)

    day = next(item for item in report["by_day"] if item["day"] == today)
    assert day["cost"] is None  # 未知 ≠ 0
    assert day["runs"] == 1
    assert day["total_tokens"] == 15
