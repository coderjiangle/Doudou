"""browser_*：门控、URL 校验、工具注解与后端交互测试（全离线）。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from doudou.config.loader import Settings
from doudou.tools.builtin import browser
from doudou.tools.builtin.browser import (
    BrowserError,
    build_chrome_launch_args,
    build_tools,
    find_chrome_executable,
    guard_navigation_url,
    missing_requirement,
    parse_local_cdp_port,
)
from doudou.tools.decorators import annotations_of
from doudou.tools.registry import build_tools as registry_build_tools
from doudou.tools.registry import describe_groups


class FakeBrowserBackend:
    """记录调用并模拟网页/播放器状态的离线后端。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def open_url(self, url: str, *, new_tab: bool = True) -> str:
        self.calls.append(("open_url", {"url": url, "new_tab": new_tab}))
        return f"已在标签页 [0] 打开：{url}"

    async def list_tabs(self) -> str:
        self.calls.append(("list_tabs", {}))
        return "* [0] 哔哩哔哩 — https://www.bilibili.com"

    async def switch_tab(self, index: int) -> str:
        self.calls.append(("switch_tab", {"index": index}))
        return f"已切换到标签页 [{index}]"

    async def close_tab(self, index: int | None = None) -> str:
        self.calls.append(("close_tab", {"index": index}))
        return f"已关闭标签页 [{index if index is not None else 0}]"

    async def read_page(self, *, filter_text: str = "", max_chars: int | None = None) -> str:
        self.calls.append(("read_page", {"filter_text": filter_text, "max_chars": max_chars}))
        return "[1] <a> 英雄联盟全球总决赛"

    async def click(self, target: str) -> str:
        self.calls.append(("click", {"target": target}))
        return f"已点击 {target!r}"

    async def type_text(self, target: str, text: str, *, submit: bool = True) -> str:
        self.calls.append(("type_text", {"target": target, "text": text, "submit": submit}))
        return f"已向 {target!r} 输入 {text!r}"

    async def scroll(self, direction: str = "down", *, pages: float = 1.0) -> str:
        self.calls.append(("scroll", {"direction": direction, "pages": pages}))
        return f"已向 {direction} 滚动"

    async def press_key(self, key: str) -> str:
        self.calls.append(("press_key", {"key": key}))
        return f"已发送按键 {key}"

    async def media_control(self, action: str = "status", *, value: float | None = None) -> str:
        self.calls.append(("media_control", {"action": action, "value": value}))
        return f"media:{action}:{value}"

    async def window_state(self, state: str = "fullscreen") -> str:
        self.calls.append(("window_state", {"state": state}))
        return f"window:{state}"


def _browser_settings(
    settings_factory: Callable[..., Settings],
    *,
    enabled: bool = True,
    allowed_domains: tuple[str, ...] = (),
) -> Settings:
    domains_yaml = ", ".join(f'"{d}"' for d in allowed_domains)
    return settings_factory(
        config_yaml=(
            "tools:\n"
            "  browser:\n"
            f"    enabled: {'true' if enabled else 'false'}\n"
            f"    allowed_domains: [{domains_yaml}]\n"
        )
    )


# --------------------------------------------------------------------------- 门控与注册


def test_disabled_by_default(settings: Settings) -> None:
    """默认配置下 browser 分组关闭，不注册任何工具。"""
    assert build_tools(settings) == []
    status = {item.name: item for item in describe_groups(settings)}["browser"]
    assert status.enabled is False
    assert "false" in status.reason


def test_skipped_when_playwright_missing(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _browser_settings(settings_factory, enabled=True)
    monkeypatch.setattr(browser, "playwright_available", lambda: False)

    assert build_tools(settings) == []
    reason = missing_requirement(settings)
    assert reason is not None
    assert "playwright" in reason


def test_tools_registered_and_annotated_when_enabled(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = _browser_settings(settings_factory, enabled=True)
    tools = {item.name: item for item in registry_build_tools(settings)}

    expected_names = {
        "browser_open",
        "browser_tabs",
        "browser_close_tab",
        "browser_read_page",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_press_key",
        "browser_media_control",
        "browser_window",
    }
    assert expected_names <= set(tools)

    # 读与媒体控制免确认；点击/输入/按键/关闭标签页标记有副作用。
    assert annotations_of(tools["browser_open"]).side_effects is False
    assert annotations_of(tools["browser_tabs"]).side_effects is False
    assert annotations_of(tools["browser_read_page"]).side_effects is False
    assert annotations_of(tools["browser_scroll"]).side_effects is False
    assert annotations_of(tools["browser_media_control"]).side_effects is False
    assert annotations_of(tools["browser_window"]).side_effects is False

    assert annotations_of(tools["browser_click"]).side_effects is True
    assert annotations_of(tools["browser_type"]).side_effects is True
    assert annotations_of(tools["browser_press_key"]).side_effects is True
    assert annotations_of(tools["browser_close_tab"]).side_effects is True


# --------------------------------------------------------------------------- URL 与启动参数校验


@pytest.mark.parametrize(
    "bad_url",
    ["", "   ", "file:///etc/passwd", "chrome://settings", "javascript:alert(1)", "https://"],
)
def test_guard_navigation_url_rejects_unsafe_urls(bad_url: str) -> None:
    with pytest.raises(BrowserError):
        guard_navigation_url(bad_url, allowed_domains=[])


def test_guard_navigation_url_enforces_optional_allowlist() -> None:
    assert (
        guard_navigation_url(
            "https://www.bilibili.com/video/BV1xx", allowed_domains=["bilibili.com"]
        )
        == "https://www.bilibili.com/video/BV1xx"
    )
    with pytest.raises(BrowserError, match="不在浏览器允许列表中"):
        guard_navigation_url("https://evil.example.com", allowed_domains=["bilibili.com"])


def test_parse_local_cdp_port() -> None:
    assert parse_local_cdp_port("http://127.0.0.1:9333") == 9333
    assert parse_local_cdp_port("http://localhost") == 9222
    with pytest.raises(BrowserError, match="不是本机地址"):
        parse_local_cdp_port("http://192.168.1.50:9222")


def test_find_chrome_executable_and_launch_args(tmp_path: Path) -> None:
    fake_exe = tmp_path / "chrome.exe"
    fake_exe.write_text("", encoding="utf-8")

    assert find_chrome_executable(str(fake_exe)) == fake_exe
    assert find_chrome_executable(str(tmp_path / "missing.exe")) is None

    argv = build_chrome_launch_args(fake_exe, port=9222, user_data_dir=tmp_path / "profile")
    assert argv[0] == str(fake_exe)
    assert "--remote-debugging-port=9222" in argv
    assert f"--user-data-dir={tmp_path / 'profile'}" in argv


# --------------------------------------------------------------------------- 离线工具调用


async def test_tools_delegate_to_backend(settings_factory: Callable[..., Settings]) -> None:
    settings = _browser_settings(settings_factory, enabled=True)
    backend = FakeBrowserBackend()
    tools = {item.name: item for item in build_tools(settings, backend=backend)}

    assert "bilibili.com" in await tools["browser_open"].ainvoke(
        {"url": "https://www.bilibili.com"}
    )
    assert "哔哩哔哩" in await tools["browser_tabs"].ainvoke({})
    assert "切换到标签页 [1]" in await tools["browser_tabs"].ainvoke({"index": 1})
    assert "英雄联盟" in await tools["browser_read_page"].ainvoke({"filter_text": "总决赛"})
    assert "已点击" in await tools["browser_click"].ainvoke({"target": "1"})
    assert "输入" in await tools["browser_type"].ainvoke(
        {"target": "搜索", "text": "英雄联盟总决赛", "submit": True}
    )
    assert "滚动" in await tools["browser_scroll"].ainvoke({"direction": "down", "pages": 1.5})
    assert "Space" in await tools["browser_press_key"].ainvoke({"key": "Space"})
    assert "media:pause:None" == await tools["browser_media_control"].ainvoke({"action": "pause"})
    assert "window:maximized" == await tools["browser_window"].ainvoke({})
    assert "window:fullscreen" == await tools["browser_window"].ainvoke({"state": "fullscreen"})
    assert "已关闭" in await tools["browser_close_tab"].ainvoke({})

    assert [name for name, _ in backend.calls] == [
        "open_url",
        "list_tabs",
        "switch_tab",
        "read_page",
        "click",
        "type_text",
        "scroll",
        "press_key",
        "media_control",
        "window_state",
        "window_state",
        "close_tab",
    ]


async def test_browser_open_rejects_bad_url_before_calling_backend(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = _browser_settings(settings_factory, enabled=True, allowed_domains=("bilibili.com",))
    backend = FakeBrowserBackend()
    tools = {item.name: item for item in build_tools(settings, backend=backend)}

    with pytest.raises(Exception, match="只支持 http:// 或 https://"):
        await tools["browser_open"].ainvoke({"url": "file:///C:/Windows/win.ini"})

    with pytest.raises(Exception, match="不在浏览器允许列表中"):
        await tools["browser_open"].ainvoke({"url": "https://other.example.com"})

    assert backend.calls == []
