"""统计聚合：把轨迹 / 审计 / 用量三份数据拼成 ``/stats`` 的响应。

纯函数，不碰数据库 —— 路由负责取行，这里只做算术，因此可以离线断言。
三个数据源各管一段（见 docs/observability.md）：

* 轨迹（``run_traces``）→ 运行数、成功率、耗时；
* 审计（``audit_events``）→ 工具调用分布、人工确认比例；
* 用量（``usage_daily``）→ token 与费用趋势。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(slots=True)
class RunSample:
    """一次运行的统计所需字段（从 ``run_traces`` 取）。"""

    started_at: datetime
    ok: bool
    elapsed: float
    tool_calls: int
    approvals: int
    rejections: int
    input_tokens: int
    output_tokens: int
    channel: str
    thread_id: str

    @property
    def day(self) -> str:
        return _day_of(self.started_at)


@dataclass(slots=True)
class ToolSample:
    """一次工具调用的统计所需字段（从 ``audit_events`` 取）。"""

    tool_name: str
    status: str
    approved: bool | None
    elapsed: float | None
    created_at: datetime


def build_stats(
    *,
    runs: Sequence[RunSample],
    tools: Sequence[ToolSample],
    usage_by_day: Mapping[str, tuple[int, int, float | None]],
    days: int,
) -> dict[str, Any]:
    """组装统计报告。

    Args:
        runs: 窗口内的运行样本。
        tools: 窗口内的工具调用样本（``audit_events`` 里 ``kind == "tool"`` 的行）。
        usage_by_day: ``天 → (input_tokens, output_tokens, cost)``；``cost`` 为
            ``None`` 表示当天有模型没配价格（未知 ≠ 0）。
        days: 统计窗口天数（用于补零，让趋势图不缺天）。
    """
    window = _window_days(days)
    by_day = _by_day(window, runs, usage_by_day)
    return {
        "days": days,
        "overview": _overview(runs),
        "by_day": by_day,
        "by_channel": _by_channel(runs),
        "tools": _tools(tools),
        "approvals": _approvals(runs, tools),
    }


def _overview(runs: Sequence[RunSample]) -> dict[str, Any]:
    total = len(runs)
    ok = sum(1 for run in runs if run.ok)
    failed = total - ok
    elapsed = [run.elapsed for run in runs]
    input_tokens = sum(run.input_tokens for run in runs)
    output_tokens = sum(run.output_tokens for run in runs)
    return {
        "runs": total,
        "ok_runs": ok,
        "failed_runs": failed,
        "success_rate": round(ok / total, 4) if total else None,
        "avg_elapsed": round(sum(elapsed) / total, 3) if total else 0.0,
        "p95_elapsed": round(_percentile(elapsed, 0.95), 3),
        "tool_calls": sum(run.tool_calls for run in runs),
        "total_tokens": input_tokens + output_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


@dataclass(slots=True)
class _DayBucket:
    runs: int = 0
    ok: int = 0
    failed: int = 0
    elapsed_sum: float = 0.0


def _by_day(
    window: Sequence[str],
    runs: Sequence[RunSample],
    usage_by_day: Mapping[str, tuple[int, int, float | None]],
) -> list[dict[str, Any]]:
    counts: dict[str, _DayBucket] = {}
    for run in runs:
        bucket = counts.setdefault(run.day, _DayBucket())
        bucket.runs += 1
        bucket.ok += 1 if run.ok else 0
        bucket.failed += 0 if run.ok else 1
        bucket.elapsed_sum += run.elapsed

    trend: list[dict[str, Any]] = []
    for day in window:
        bucket = counts.get(day, _DayBucket())
        input_tokens, output_tokens, cost = usage_by_day.get(day, (0, 0, 0.0))
        trend.append(
            {
                "day": day,
                "runs": bucket.runs,
                "ok": bucket.ok,
                "failed": bucket.failed,
                "avg_elapsed": round(bucket.elapsed_sum / bucket.runs, 3) if bucket.runs else 0.0,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cost": cost,
            }
        )
    return trend


def _by_channel(runs: Sequence[RunSample]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, int]] = {}
    for run in runs:
        key = run.channel or "unknown"
        bucket = buckets.setdefault(key, {"runs": 0, "ok": 0, "failed": 0})
        bucket["runs"] += 1
        bucket["ok" if run.ok else "failed"] += 1
    return [
        {"channel": channel, **bucket}
        for channel, bucket in sorted(
            buckets.items(), key=lambda item: item[1]["runs"], reverse=True
        )
    ]


def _tools(tools: Sequence[ToolSample]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for tool in tools:
        name = tool.tool_name or "unknown"
        bucket = buckets.setdefault(
            name,
            {"name": name, "calls": 0, "ok": 0, "failed": 0, "rejected": 0, "_elapsed": []},
        )
        bucket["calls"] += 1
        if tool.status == "rejected":
            bucket["rejected"] += 1
        elif tool.status == "failed":
            bucket["failed"] += 1
        else:
            bucket["ok"] += 1
        if tool.elapsed is not None:
            bucket["_elapsed"].append(float(tool.elapsed))

    result: list[dict[str, Any]] = []
    for bucket in sorted(buckets.values(), key=lambda item: item["calls"], reverse=True):
        elapsed = bucket.pop("_elapsed")
        bucket["avg_elapsed"] = round(sum(elapsed) / len(elapsed), 4) if elapsed else None
        result.append(bucket)
    return result


def _approvals(runs: Sequence[RunSample], tools: Sequence[ToolSample]) -> dict[str, Any]:
    """人工确认统计。

    ``approved is None`` 表示**该工具不需要确认**（根本没问过）—— 必须和「同意了」
    分开算，否则会报告「全都经过人确认」，那是假的。以工具维度为准（更细），
    运行维度只作交叉校验。
    """
    required = [tool for tool in tools if tool.approved is not None]
    approvals = sum(1 for tool in required if tool.approved)
    rejections = len(required) - approvals
    return {
        "required": len(required),
        "approvals": approvals,
        "rejections": rejections,
        "auto": len(tools) - len(required),
        "approval_rate": round(approvals / len(required), 4) if required else None,
    }


def _window_days(days: int) -> list[str]:
    """最近 ``days`` 天（含今天），按时间升序。"""
    today = datetime.now(UTC).date()
    span = max(1, days)
    return [(today - timedelta(days=offset)).isoformat() for offset in range(span - 1, -1, -1)]


def _percentile(values: Sequence[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


def _day_of(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).date().isoformat()


__all__ = ["RunSample", "ToolSample", "build_stats"]
