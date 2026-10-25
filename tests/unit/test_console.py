"""控制台编码适配：Windows 上按「终端 / 管道」分别处理，其它平台不做事。"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from doudou.utils import console


class FakeStream:
    def __init__(self, *, terminal: bool = False, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.terminal = terminal
        self.fail = fail

    def isatty(self) -> bool:
        return self.terminal

    def reconfigure(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.fail:
            raise OSError("stream is detached")


def test_noop_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    out, err = FakeStream(terminal=True), FakeStream(terminal=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    console.configure_stdio()

    assert out.calls == []
    assert err.calls == []


def test_terminal_switches_to_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(console, "_set_windows_code_page", lambda: None)
    out, err = FakeStream(terminal=True), FakeStream(terminal=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    console.configure_stdio()

    expected = {"encoding": "utf-8", "errors": "replace"}
    assert out.calls == [expected]
    assert err.calls == [expected]


def test_pipe_keeps_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """管道/重定向时保留系统编码，只把不可编码字符替换掉。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(console, "_set_windows_code_page", lambda: None)
    out = FakeStream(terminal=False)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", FakeStream(terminal=False))

    console.configure_stdio()

    assert out.calls == [{"errors": "replace"}]


def test_stream_without_reconfigure_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试或嵌入场景下 stdout 可能不是 TextIOWrapper，不能因此崩掉。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(console, "_set_windows_code_page", lambda: None)
    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())

    console.configure_stdio()


def test_reconfigure_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(console, "_set_windows_code_page", lambda: None)
    monkeypatch.setattr(sys, "stdout", FakeStream(terminal=True, fail=True))
    monkeypatch.setattr(sys, "stderr", FakeStream(terminal=True, fail=True))

    console.configure_stdio()
