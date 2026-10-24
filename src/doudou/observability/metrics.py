"""Counters and timers for runs, tools and channels.

刻意做成极简的进程内实现：M2 需要的只是「跑过多少次、慢在哪」，
真正的指标体系（Prometheus / OTel）放到 M9 生产化。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(slots=True)
class MetricsSnapshot:
    """某一时刻的指标快照。"""

    counters: dict[str, int] = field(default_factory=dict)
    durations: dict[str, dict[str, float]] = field(default_factory=dict)


class Metrics:
    """线程安全的计数器与耗时统计。

    指标名约定 ``<域>.<对象>.<动作>``，例如 ``run.finished``、``tool.now.ok``。
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._durations: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def incr(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] += value

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            self._durations[name].append(seconds)

    def snapshot(self, *, keep_recent: int = 200) -> MetricsSnapshot:
        """导出快照；耗时只保留最近 ``keep_recent`` 次，避免无界增长。"""
        with self._lock:
            counters = dict(self._counters)
            durations: dict[str, dict[str, float]] = {}
            for name, values in self._durations.items():
                recent = values[-keep_recent:]
                durations[name] = {
                    "count": float(len(recent)),
                    "avg_ms": round(sum(recent) / len(recent) * 1000, 2),
                    "max_ms": round(max(recent) * 1000, 2),
                }
        return MetricsSnapshot(counters=counters, durations=durations)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._durations.clear()


_metrics = Metrics()


def get_metrics() -> Metrics:
    """进程级指标实例。"""
    return _metrics


__all__ = ["Metrics", "MetricsSnapshot", "get_metrics"]
