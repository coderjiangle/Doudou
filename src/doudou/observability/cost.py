"""成本核算：按会话、按天统计 token 与费用，并在超预算时告警。

## 钱算得准不准，取决于价格表

价格是**部署相关的**（自建网关、企业折扣、阶梯价），所以不写死在代码里，
而是走配置：``observability.cost.prices``，键是 ``provider:model`` 或模型名，
``default`` 兜底。

查不到价格时**不猜**：``cost`` 记为 ``None``，同时把「未知价格的模型」列出来
（``/api/v1/usage`` 里有 ``unknown_models``）。这一点很重要 ——
编一个数字出来比不显示更糟，因为它会让人以为成本在掌控之中。

## 为什么告警而不拦截

超预算就拒绝服务听起来很爽，但代价是「用户的消息没人回」。所以这里只做：
日志告警（每天一次）+ 指标计数 + 在 ``/usage`` 与 ``/health`` 里显示 ``exceeded``。
真要拦，应该在网关或模型侧限流/限额，而不是在助手的编排层。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from doudou.observability.logging import get_logger
from doudou.observability.metrics import get_metrics
from doudou.observability.observers import BaseObserver, RunContext, RunSummary

logger = get_logger(__name__)

#: 价格按「每 100 万 token」计价（各家报价的通用口径，避免小数点后一串零）。
PRICE_UNIT = 1_000_000


@dataclass(frozen=True, slots=True)
class Price:
    """每 100 万 token 的价格。"""

    input: float = 0.0
    output: float = 0.0

    def cost(self, *, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input + output_tokens * self.output) / PRICE_UNIT


class PriceTable:
    """模型 → 价格。精确名优先，其次「模型短名」，最后 ``default``。"""

    def __init__(self, prices: dict[str, dict[str, float]] | None = None) -> None:
        self._prices: dict[str, Price] = {}
        for key, value in (prices or {}).items():
            if key == "default":
                continue
            price = Price(
                input=float(value.get("input", 0.0)), output=float(value.get("output", 0.0))
            )
            normalized = key.strip().lower()
            self._prices[normalized] = price
            # 同时按「模型短名」建一份索引：配置里写 ``openai:gpt-x`` 的人，
            # 未必知道 ``settings.app.model.primary`` 里带不带 provider 前缀。
            # 两个 provider 撞同一个短名时后者覆盖前者 —— 那种情况下请写全名。
            short = normalized.split(":", 1)[-1]
            if short and short != normalized:
                self._prices.setdefault(short, price)
        raw_default = (prices or {}).get("default")
        self._default = (
            Price(
                input=float(raw_default.get("input", 0.0)),
                output=float(raw_default.get("output", 0.0)),
            )
            if raw_default
            else None
        )

    @property
    def known(self) -> list[str]:
        return sorted(self._prices)

    def price_for(self, model: str) -> Price | None:
        """查价格：先用 ``provider:model``，再用模型短名，最后 ``default``。"""
        key = (model or "").strip().lower()
        if not key:
            return self._default
        if key in self._prices:
            return self._prices[key]
        short = key.split(":", 1)[-1]
        if short in self._prices:
            return self._prices[short]
        return self._default

    def cost(self, model: str, *, input_tokens: int, output_tokens: int) -> float | None:
        price = self.price_for(model)
        if price is None:
            return None
        return round(price.cost(input_tokens=input_tokens, output_tokens=output_tokens), 6)


@dataclass(slots=True)
class UsageRecord:
    """某一天、某条会话的累计用量。"""

    day: str
    thread_id: str
    channel: str = ""
    model: str = ""
    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    approvals: int = 0
    rejections: int = 0
    #: 累计费用（美元）。``None`` 表示有运行的价格查不到 —— 不要把 None 当 0。
    cost: float | None = 0.0
    last_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "thread_id": self.thread_id,
            "channel": self.channel,
            "model": self.model,
            "runs": self.runs,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls,
            "approvals": self.approvals,
            "rejections": self.rejections,
            "cost": self.cost,
            "last_at": self.last_at.isoformat(),
        }


class UsageStore(Protocol):
    """用量落点。"""

    async def add(self, record: UsageRecord) -> None:
        """把一条记录累加进对应的 (day, thread_id)。"""
        ...

    async def add_many(self, records: list[UsageRecord]) -> None:
        """批量累加（一次提交）。这层是为攒批写准备的，见 ``buffering.py``。"""
        ...

    async def by_thread(self, *, days: int) -> list[UsageRecord]: ...

    async def by_day(self, *, days: int) -> list[UsageRecord]:
        """按天汇总（``thread_id`` 为 ``*``）。"""
        ...

    async def total(
        self, *, day: str | None = None, include_pending: bool = False
    ) -> tuple[int, int, float | None]:
        """``(input_tokens, output_tokens, cost)``。``day`` 为 ``None`` 表示全部。

        ``include_pending`` 只有攒批写那一层（``buffering.py``）会用到；
        这里的实现没有缓冲区，参数只为满足协议。
        """
        ...


class MemoryUsageStore:
    """进程内用量统计（单副本部署与测试用）。"""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], UsageRecord] = {}

    async def add(self, record: UsageRecord) -> None:
        key = (record.day, record.thread_id)
        current = self._records.get(key)
        if current is None:
            self._records[key] = record
            return
        current.runs += record.runs
        current.input_tokens += record.input_tokens
        current.output_tokens += record.output_tokens
        current.tool_calls += record.tool_calls
        current.approvals += record.approvals
        current.rejections += record.rejections
        current.cost = _add_cost(current.cost, record.cost)
        current.model = record.model or current.model
        current.channel = record.channel or current.channel
        current.last_at = record.last_at

    async def add_many(self, records: list[UsageRecord]) -> None:
        for record in records:
            await self.add(record)

    async def by_thread(self, *, days: int) -> list[UsageRecord]:
        allowed = _recent_days(days)
        merged: dict[str, UsageRecord] = {}
        for (day, _thread), record in self._records.items():
            if day not in allowed:
                continue
            bucket = merged.get(record.thread_id)
            if bucket is None:
                merged[record.thread_id] = _copy(record, day="*")
                continue
            bucket.runs += record.runs
            bucket.input_tokens += record.input_tokens
            bucket.output_tokens += record.output_tokens
            bucket.tool_calls += record.tool_calls
            bucket.approvals += record.approvals
            bucket.rejections += record.rejections
            bucket.cost = _add_cost(bucket.cost, record.cost)
        return sorted(merged.values(), key=lambda item: item.total_tokens, reverse=True)

    async def by_day(self, *, days: int) -> list[UsageRecord]:
        allowed = _recent_days(days)
        merged: dict[str, UsageRecord] = {}
        for (day, _thread), record in self._records.items():
            if day not in allowed:
                continue
            bucket = merged.get(day)
            if bucket is None:
                merged[day] = _copy(record, thread_id="*")
                continue
            bucket.runs += record.runs
            bucket.input_tokens += record.input_tokens
            bucket.output_tokens += record.output_tokens
            bucket.tool_calls += record.tool_calls
            bucket.cost = _add_cost(bucket.cost, record.cost)
        return sorted(merged.values(), key=lambda item: item.day, reverse=True)

    async def total(
        self, *, day: str | None = None, include_pending: bool = False
    ) -> tuple[int, int, float | None]:
        input_tokens = output_tokens = 0
        cost: float | None = 0.0
        for (record_day, _thread), record in self._records.items():
            if day is not None and record_day != day:
                continue
            input_tokens += record.input_tokens
            output_tokens += record.output_tokens
            cost = _add_cost(cost, record.cost)
        return input_tokens, output_tokens, cost


def _copy(
    record: UsageRecord, *, day: str | None = None, thread_id: str | None = None
) -> UsageRecord:
    return UsageRecord(
        day=record.day if day is None else day,
        thread_id=record.thread_id if thread_id is None else thread_id,
        channel=record.channel,
        model=record.model,
        runs=record.runs,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        tool_calls=record.tool_calls,
        approvals=record.approvals,
        rejections=record.rejections,
        cost=record.cost,
        last_at=record.last_at,
    )


def _add_cost(left: float | None, right: float | None) -> float | None:
    """合并费用。**任何一边未知，结果就是未知** —— 把 ``None`` 当 0 会少算钱。"""
    if left is None or right is None:
        return None
    return round(left + right, 6)


def _recent_days(days: int) -> set[str]:
    from datetime import timedelta

    today = datetime.now(UTC).date()
    return {(today - timedelta(days=offset)).isoformat() for offset in range(max(1, days))}


def today_key(now: date | None = None) -> str:
    return (now or datetime.now(UTC).date()).isoformat()


class UsageObserver(BaseObserver):
    """把每次运行的 token 与费用记进用量表，并在超预算时告警。"""

    name = "usage"

    def __init__(
        self,
        store: UsageStore,
        *,
        model: str = "",
        prices: PriceTable | None = None,
        daily_budget: float = 0.0,
    ) -> None:
        self.store = store
        self.model = model
        self.prices = prices or PriceTable()
        self.daily_budget = max(0.0, daily_budget)
        self._warned_on: str | None = None
        self._unknown: set[str] = set()

    @property
    def unknown_models(self) -> list[str]:
        """查不到价格、因此费用未知的模型。"""
        return sorted(self._unknown)

    async def on_run_end(self, context: RunContext, summary: RunSummary) -> None:
        input_tokens = int(summary.usage.get("input_tokens", 0) or 0)
        output_tokens = int(summary.usage.get("output_tokens", 0) or 0)
        cost = self.prices.cost(self.model, input_tokens=input_tokens, output_tokens=output_tokens)
        if cost is None and self.model:
            if self.model not in self._unknown:
                self._unknown.add(self.model)
                logger.warning(
                    "cost.price_unknown",
                    model=self.model,
                    hint="cost 记为空；请在 observability.cost.prices 里补上这个模型的价格",
                )

        record = UsageRecord(
            day=today_key(),
            thread_id=context.thread_id,
            channel=context.channel,
            model=self.model,
            runs=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=summary.tool_calls,
            approvals=summary.approvals,
            rejections=summary.rejections,
            cost=cost,
        )
        await self.store.add(record)
        get_metrics().incr("cost.runs")
        get_metrics().incr("cost.input_tokens", input_tokens)
        get_metrics().incr("cost.output_tokens", output_tokens)
        await self._check_budget()

    async def _check_budget(self) -> None:
        if self.daily_budget <= 0:
            return
        day = today_key()
        _input, _output, cost = await self.store.total(day=day, include_pending=True)
        if cost is None:
            return  # 费用未知时不告警：告警的前提是知道数字
        if cost <= self.daily_budget:
            return
        get_metrics().incr("cost.budget_exceeded")
        if self._warned_on == day:
            return  # 每天只说一次，否则每次运行都在喊
        self._warned_on = day
        logger.warning(
            "cost.budget_exceeded",
            day=day,
            cost=round(cost, 4),
            budget=self.daily_budget,
            hint="仅告警不拦截；需要硬限制请在网关/模型侧限流",
        )


def build_usage_report(
    *,
    records_by_day: Iterable[UsageRecord],
    records_by_thread: Iterable[UsageRecord],
    total: tuple[int, int, float | None],
    today: tuple[int, int, float | None],
    model: str,
    budget: float,
    unknown_models: list[str],
    prices_known: list[str],
) -> dict[str, Any]:
    """组装 ``GET /usage`` 的响应体。

    单独抽出来是为了能离线断言「预算判定的边界」与「未知费用不被当成 0」
    这两件容易写错的事。
    """
    input_tokens, output_tokens, cost = total
    today_input, today_output, today_cost = today
    exceeded = bool(budget > 0 and today_cost is not None and today_cost > budget)
    return {
        "model": model,
        "price_unit_tokens": PRICE_UNIT,
        "prices_known": prices_known,
        "unknown_models": unknown_models,
        "total": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost": cost,
        },
        "today": {
            "day": today_key(),
            "input_tokens": today_input,
            "output_tokens": today_output,
            "total_tokens": today_input + today_output,
            "cost": today_cost,
        },
        "by_day": [item.as_dict() for item in records_by_day],
        "by_thread": [item.as_dict() for item in records_by_thread],
        "budget": {
            "daily": budget,
            "exceeded": exceeded,
            "cost_known": today_cost is not None,
        },
        "note": (
            "费用按 observability.cost.prices 估算，不是账单。"
            "cost 为 null 表示该模型没配价格（未知 ≠ 0）。"
            "超预算只告警，不拦截。"
        ),
    }


__all__ = [
    "PRICE_UNIT",
    "MemoryUsageStore",
    "Price",
    "PriceTable",
    "UsageObserver",
    "UsageRecord",
    "UsageStore",
    "build_usage_report",
    "today_key",
]
