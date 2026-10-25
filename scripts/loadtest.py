"""离线压测：测的是**编排开销**，不是模型速度。

## 为什么不用真模型

真模型下测出来的数字由「上游网关的排队与吞吐」决定，换一天就变，也复现不了。
这个脚本要回答的是另一个问题：**我们自己这一层每秒能推多少轮**
（历史读写 + 上下文裁剪 + 工具执行 + 观察者写入）。

因此它用一个确定性模型：每轮「先调一次 ``now`` 再给结论」。与 ``scripted:demo``
同源，但它**无状态且可重复**——多会话并发复用同一个实例不会串味。

## 读数的用法

* ``runs_per_second``：单进程编排吞吐。真模型下这个数字远高于实际请求量，
  所以**它不是瓶颈**，它的意义是「编排不是瓶颈」这个结论本身；
* ``latency_ms.p95``：一轮编排的尾延迟，决定「同一会话连续提问的体感」；
* ``bus.rejected``：会话积压超过上限时被拒的次数 —— 这是**有意的背压**
  （见 ``gateway/bus.py``）。看的就是它从 0 变成非 0 的那个并发点。

## 用法

```bash
uv run python scripts/loadtest.py --concurrency 16 --turns 20
uv run python scripts/loadtest.py --concurrency 64 --turns 5 --database   # 带 SQLite 写入
uv run python scripts/loadtest.py --concurrency 16 --turns 20 --no-tool
```
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from doudou.agent.factory import build_agent
from doudou.agent.testing import ScriptedChatModel, tool_call
from doudou.channels.base import InboundMessage
from doudou.config.loader import load_settings
from doudou.gateway.bus import MessageBus
from doudou.gateway.history import InMemoryHistoryStore
from doudou.gateway.router import MessageRouter
from doudou.gateway.runner import AgentRunner
from doudou.observability.setup import build_observability

#: 默认配置目录：仓库里的 config/。用真实提示词与工具面才测得出真实开销。
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


class LoadModel(ScriptedChatModel):
    """每轮「先调一次工具、再给结论」的确定性模型。

    只看上下文尾部有没有工具结果，因此**无状态**：
    ``ScriptedChatModel`` 默认按「已经出现过几条 AIMessage」推进会，
    并发复用会串味，这里换成按内容判断。
    """

    fallback_text: str = "结论：已完成。"

    def _next_message(self, messages: list[BaseMessage]) -> AIMessage:
        """只看**本轮**（最后一条 ``HumanMessage`` 之后）有没有工具结果。

        不能只看「最近 4 条」：上一轮的历史里也躺着 ToolMessage，
        那样模型会从第二轮起直接给结论，工具调用一次都测不到。
        """
        last_human = -1
        for index, message in enumerate(messages):
            if not isinstance(message, AIMessage) and message.type == "human":
                last_human = index
        tail = messages[last_human + 1 :]
        if any(isinstance(message, ToolMessage) for message in tail):
            return AIMessage(content=self.fallback_text)
        return tool_call("now", {"timezone": "UTC"}, call_id=f"c{len(messages)}")


class NullSink:
    """什么都不渲染的 sink：压测关心编排，不关心渲染与网络。"""

    name = "loadtest"

    def __init__(self) -> None:
        self._done = asyncio.Event()

    async def send(self, message: Any) -> None:
        return None

    async def deliver(self, message: Any, events: Any) -> None:
        try:
            async for _event in events:
                pass
        finally:
            # 无论正常结束还是被取消，都让等待方醒过来，否则压测会挂住。
            self._done.set()

    async def wait(self) -> None:
        await self._done.wait()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Doudou 离线压测（编排层）")
    parser.add_argument("--concurrency", type=int, default=8, help="并发会话数")
    parser.add_argument("--turns", type=int, default=10, help="每个会话的轮数")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument(
        "--database", action="store_true", help="用 SQLite 落历史与审计（默认全内存）"
    )
    parser.add_argument("--no-tool", action="store_true", help="模型不调工具（只测纯文本轮次）")
    parser.add_argument("--out", type=Path, default=None, help="把结果写成 JSON")
    return parser.parse_args(argv)


async def build(*, config_dir: Path, database: bool, with_tool: bool):  # type: ignore[no-untyped-def]
    """装配一个尽量贴近真实的 runner（含观察者）。"""
    os.environ.setdefault("DOUDOU_RUNTIME__LOG_LEVEL", "ERROR")
    settings = load_settings(config_dir=config_dir, use_env=True)

    model = LoadModel() if with_tool else ScriptedChatModel(fallback_text="结论：已完成。")
    bundle = build_agent(settings, model=model)

    db = None
    store: Any = InMemoryHistoryStore()
    if database:
        from doudou.storage.database import create_database
        from doudou.storage.history import SqlHistoryStore

        db = create_database(settings.app.storage)
        await db.create_schema()
        store = SqlHistoryStore(db, max_messages=settings.app.storage.max_messages_per_thread)

    observability = build_observability(settings, database=db)
    runner = AgentRunner(bundle, store=store, observers=observability.observers)
    return runner, observability, db


async def run_session(router: MessageRouter, index: int, turns: int, latencies: list[float]) -> int:
    """跑一条会话的若干轮，返回被接受的轮数。"""
    message = InboundMessage(channel="loadtest", conversation_id=f"c{index}", text="现在几点？")
    accepted_count = 0
    for _ in range(turns):
        sink = NullSink()
        started = time.perf_counter()
        accepted = await router.dispatch(message, sink)
        if accepted:
            await sink.wait()
        latencies.append((time.perf_counter() - started) * 1000)
        accepted_count += int(accepted)
    return accepted_count


async def main(argv: list[str]) -> int:
    args = parse_args(argv)
    runner, observability, db = await build(
        config_dir=args.config_dir, database=args.database, with_tool=not args.no_tool
    )
    bus = MessageBus()
    await bus.start()
    router = MessageRouter(runner, bus=bus)

    # 预热一轮：第一次运行要付「加载记忆 + 扫 Skill + 首次上下文准备」的钱，
    # 算进吞吐里会把固定成本摊到并发数上，数字就不可比了。
    await run_session(router, 0, 1, [])

    latencies: list[float] = []
    started = time.perf_counter()
    await asyncio.gather(
        *(run_session(router, index, args.turns, latencies) for index in range(args.concurrency))
    )
    elapsed = time.perf_counter() - started

    # 先把要报告的读数摘出来，再关资源 —— aclose() 会清空 observers 列表。
    summary = {
        "observers": observability.names,
        "tools": len(runner.bundle.tools),
        "bus": bus.stats.as_dict(),
    }

    await bus.stop()
    await observability.aclose()
    if db is not None:
        await db.dispose()

    latencies.sort()
    result = {
        "concurrency": args.concurrency,
        "turns_per_session": args.turns,
        "runs": len(latencies),
        "wall_seconds": round(elapsed, 3),
        "runs_per_second": round(len(latencies) / elapsed, 1) if elapsed else None,
        "latency_ms": {
            "p50": round(_percentile(latencies, 50), 2),
            "p95": round(_percentile(latencies, 95), 2),
            "p99": round(_percentile(latencies, 99), 2),
            "max": round(max(latencies), 2) if latencies else None,
        },
        "bus": summary["bus"],
        "history": "database" if args.database else "memory",
        "tools": summary["tools"],
        "observers": summary["observers"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out is not None:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def _percentile(values: list[float], percent: float) -> float:
    """线性插值分位数。样本少时比 nearest-rank 更稳。"""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
