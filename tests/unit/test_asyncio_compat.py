"""Windows Proactor 连接重置噪音过滤：只吞传输关闭回调上的已知异常。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from doudou.utils import asyncio_compat


class FakeLoop:
    def __init__(self, handler: Callable[[Any, dict[str, Any]], None] | None = None) -> None:
        self.handler = handler
        self.default_contexts: list[dict[str, Any]] = []

    def get_exception_handler(self) -> Callable[[Any, dict[str, Any]], None] | None:
        return self.handler

    def set_exception_handler(self, handler: Callable[[Any, dict[str, Any]], None]) -> None:
        self.handler = handler

    def default_exception_handler(self, context: dict[str, Any]) -> None:
        self.default_contexts.append(context)


def _handle(callback_name: str) -> SimpleNamespace:
    return SimpleNamespace(_callback=SimpleNamespace(__name__=callback_name))


def _reset_context(*, callback: str = "_call_connection_lost") -> dict[str, Any]:
    return {
        "message": "Exception in callback",
        "handle": _handle(callback),
        "exception": ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接"),
        "transport": object(),
    }


def test_filter_swallows_connection_reset_on_transport_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    loop = FakeLoop()

    asyncio_compat.install_connection_reset_filter(loop)  # type: ignore[arg-type]
    assert loop.handler is not None
    loop.handler(loop, _reset_context())

    assert loop.default_contexts == []


def test_filter_keeps_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    loop = FakeLoop()
    asyncio_compat.install_connection_reset_filter(loop)  # type: ignore[arg-type]

    other = {"message": "unhandled", "exception": RuntimeError("boom")}
    loop.handler(loop, other)  # type: ignore[misc]

    assert loop.default_contexts == [other]


def test_filter_keeps_reset_outside_transport_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """读请求体等路径的连接重置必须暴露出来，不能当成关闭噪音。"""
    monkeypatch.setattr(sys, "platform", "win32")
    loop = FakeLoop()
    asyncio_compat.install_connection_reset_filter(loop)  # type: ignore[arg-type]

    context = _reset_context(callback="_loop_reading")
    loop.handler(loop, context)  # type: ignore[misc]

    assert loop.default_contexts == [context]


def test_filter_delegates_to_previous_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    seen: list[dict[str, Any]] = []
    loop = FakeLoop(handler=lambda _loop, context: seen.append(context))

    asyncio_compat.install_connection_reset_filter(loop)  # type: ignore[arg-type]
    loop.handler(loop, _reset_context())  # type: ignore[misc]
    other = {"message": "unhandled", "exception": ValueError("x")}
    loop.handler(loop, other)  # type: ignore[misc]

    assert seen == [other]


def test_filter_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    loop = FakeLoop()

    asyncio_compat.install_connection_reset_filter(loop)  # type: ignore[arg-type]
    first = loop.handler
    asyncio_compat.install_connection_reset_filter(loop)  # type: ignore[arg-type]

    assert loop.handler is first
    loop.handler(loop, _reset_context())  # type: ignore[misc]
    assert loop.default_contexts == []


def test_noop_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    loop = FakeLoop()

    asyncio_compat.install_connection_reset_filter(loop)  # type: ignore[arg-type]

    assert loop.handler is None


def test_run_async_installs_filter_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def fake_install() -> None:
        calls.append(True)

    monkeypatch.setattr(asyncio_compat, "install_connection_reset_filter", fake_install)

    async def main() -> int:
        await asyncio.sleep(0)
        return 7

    assert asyncio_compat.run_async(main()) == 7
    assert calls == [True]


def test_safe_self_pipe_close_handles_partial_and_repeated_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_close_self_pipe`` 在 ``_ssock`` 已被置空后再次调用不能抛 AttributeError。"""
    import asyncio.proactor_events as proactor_events

    monkeypatch.setattr(sys, "platform", "win32")

    closed: list[str] = []

    class FakeSock:
        def __init__(self, label: str) -> None:
            self.label = label

        def close(self) -> None:
            closed.append(self.label)

    class FakeFuture:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    class DummyProactorLoop:
        def _close_self_pipe(self) -> None:
            self._ssock.close()  # type: ignore[attr-defined]
            self._ssock = None
            self._csock.close()  # type: ignore[attr-defined]
            self._csock = None

    monkeypatch.setattr(proactor_events, "BaseProactorEventLoop", DummyProactorLoop)
    asyncio_compat.install_safe_self_pipe_close()

    loop = DummyProactorLoop()
    fut = FakeFuture()
    loop._self_reading_future = fut  # type: ignore[attr-defined]
    loop._ssock = FakeSock("s")  # type: ignore[attr-defined]
    loop._csock = FakeSock("c")  # type: ignore[attr-defined]

    # 正常情况走原方法并清掉双端 socket
    loop._close_self_pipe()
    assert closed == ["s", "c"]
    assert loop._ssock is None  # type: ignore[attr-defined]

    # 模拟关闭途中被 Ctrl+C 打断后 __del__ 二次关闭：不抛 AttributeError
    loop._self_reading_future = fut  # type: ignore[attr-defined]
    loop._close_self_pipe()
    assert fut.cancelled is True

    # 只有单边残存时也安全收掉
    loop._csock = FakeSock("c2")  # type: ignore[attr-defined]
    loop._close_self_pipe()
    assert closed == ["s", "c", "c2"]
    assert loop._csock is None  # type: ignore[attr-defined]
