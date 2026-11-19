"""Chrome browser control tools via Playwright CDP.

默认关闭（``tools.browser.enabled: false``），需要操控本机 Google Chrome 时再启用。

设计要点：

* **连接本机 Chrome（CDP）**：优先连接 ``tools.browser.cdp_url``（默认
  ``http://127.0.0.1:9222``）；端口未监听且 ``auto_launch: true`` 时自动拉起一个
  独立的 Chrome 进程并持久化 ``user_data_dir``，豆豆退出后浏览器窗口依然保留，
  登录态与正在播放的视频不会中断。
* **文本与媒体优先**：``browser_read_page`` 只返回精简正文、媒体状态和带编号的
  可见交互元素（而非整页原始 HTML），节省上下文 token；``browser_media_control``
  直接操作 HTML5 ``<video>`` / ``<audio>``，哪怕浏览器不在前台也能秒级暂停/调音量。
* **自动切到新标签页**：视频站（如 B 站）卡片常带 ``target="_blank"``，点击后
  检测到新标签页会自动切换焦点，避免模型继续停留在旧页面。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings
from doudou.config.schema import BrowserToolConfig
from doudou.observability.logging import get_logger
from doudou.tools.builtin.http import domain_allowed
from doudou.tools.decorators import annotate
from doudou.utils.text import truncate

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Locator, Page, Playwright

logger = get_logger(__name__)

GROUP = "browser"

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_CDP_PORT = 9222
_MAX_INTERACTIVE_ELEMENTS = 60

MEDIA_ACTIONS = frozenset(
    {
        "status",
        "play",
        "pause",
        "toggle",
        "mute",
        "unmute",
        "set_volume",
        "seek",
        "forward",
        "backward",
        "speed",
        "fullscreen",
        "exit_fullscreen",
    }
)
SCROLL_DIRECTIONS = frozenset({"down", "up", "top", "bottom"})
WINDOW_STATES = frozenset({"fullscreen", "maximized", "normal", "minimized"})

_FULLSCREEN_BUTTON_SELECTORS = (
    ".bpx-player-ctrl-full",
    ".ytp-fullscreen-button",
    'button[aria-label*="全屏" i]',
    'button[title*="全屏" i]',
    '[role="button"][aria-label*="全屏" i]',
    'button[aria-label*="fullscreen" i]',
    'button[title*="fullscreen" i]',
)


class BrowserError(ValueError):
    """浏览器操作失败或参数非法。消息会直接回填给模型，必须可操作。"""


class BrowserBackend(Protocol):
    """浏览器控制后端接口；测试可注入假实现以完全离线运行。"""

    async def open_url(self, url: str, *, new_tab: bool = True) -> str: ...

    async def list_tabs(self) -> str: ...

    async def switch_tab(self, index: int) -> str: ...

    async def close_tab(self, index: int | None = None) -> str: ...

    async def read_page(self, *, filter_text: str = "", max_chars: int | None = None) -> str: ...

    async def click(self, target: str) -> str: ...

    async def type_text(self, target: str, text: str, *, submit: bool = True) -> str: ...

    async def scroll(self, direction: str = "down", *, pages: float = 1.0) -> str: ...

    async def press_key(self, key: str) -> str: ...

    async def media_control(self, action: str = "status", *, value: float | None = None) -> str: ...

    async def window_state(self, state: str = "fullscreen") -> str: ...


def playwright_available() -> bool:
    """检查当前环境是否已安装 ``playwright``。"""
    return importlib.util.find_spec("playwright") is not None


def missing_requirement(settings: Settings) -> str | None:
    """browser 分组不可用的原因；可用时返回 ``None``。"""
    config = settings.app.tools.browser
    if not config.enabled:
        return "tools.browser.enabled 为 false（默认关闭）"
    if not playwright_available():
        return "未安装 playwright 依赖（请运行 uv add playwright 或 uv sync）"
    return None


def guard_navigation_url(url: str, *, allowed_domains: list[str]) -> str:
    """校验待访问 URL：只允许 http/https，且若配置了白名单则必须命中。"""
    cleaned = url.strip()
    if not cleaned:
        raise BrowserError("网址不能为空，请给出完整的 http:// 或 https:// 地址。")
    parts = urlsplit(cleaned)
    if parts.scheme.lower() not in {"http", "https"}:
        raise BrowserError(f"只支持 http:// 或 https:// 网址，收到 {parts.scheme or '（空）'!r}。")
    host = parts.hostname or ""
    if not host:
        raise BrowserError("网址里没有主机名，请给出完整地址。")
    if allowed_domains and not domain_allowed(host, allowed_domains):
        raise BrowserError(
            f"域名 {host} 不在浏览器允许列表中，已拒绝访问。"
            f"当前允许：{'、'.join(allowed_domains)}。"
        )
    return cleaned


def find_chrome_executable(configured: str | None = None) -> Path | None:
    """查找本机 Google Chrome 可执行文件。"""
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_file() else None

    candidates: list[Path] = []
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidates.extend(
            [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            ]
        )
        if local_app_data:
            candidates.append(
                Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                return Path(found)

    for path in candidates:
        if path.is_file():
            return path
    which_chrome = shutil.which("chrome")
    return Path(which_chrome) if which_chrome else None


def parse_local_cdp_port(cdp_url: str) -> int:
    """解析本机 CDP 端口；非本机地址时抛 :class:`BrowserError`。"""
    parts = urlsplit(cdp_url.strip())
    host = (parts.hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        raise BrowserError(
            f"cdp_url ({cdp_url}) 不是本机地址，无法自动拉起本地 Chrome。"
            "请先在远端启动调试端口，或将 tools.browser.auto_launch 设为 false。"
        )
    return parts.port or _DEFAULT_CDP_PORT


def build_chrome_launch_args(
    executable: Path,
    *,
    port: int,
    user_data_dir: Path,
) -> list[str]:
    """构造独立 Chrome 进程的启动参数。"""
    return [
        str(executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--start-maximized",
        "--no-first-run",
        "--no-default-browser-check",
    ]


class PlaywrightBrowserBackend:
    """基于 Playwright CDP 的本机 Chrome 控制后端。"""

    def __init__(self, config: BrowserToolConfig, *, cwd: Path | None = None) -> None:
        self.config = config
        self.base_cwd = cwd or Path.cwd()
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._active_page: Page | None = None

    @property
    def _timeout_ms(self) -> float:
        return max(1.0, float(self.config.timeout)) * 1000.0

    # ------------------------------------------------------------------ 连接与生命周期

    async def _ensure_page(self) -> tuple[BrowserContext, Page]:
        """确保已连接到 Chrome，并返回 ``(context, active_page)``。"""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # 事件循环已切换（例如独立 CLI / 多轮测试），丢弃旧引用并重连。
            self._playwright = None
            self._browser = None
            self._active_page = None
            self._loop = loop

        if self._browser is None or not self._browser.is_connected():
            await self._connect()

        assert self._browser is not None
        context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else await self._browser.new_context()
        )
        pages = [page for page in context.pages if not page.is_closed()]
        if self._active_page is not None and self._active_page in pages:
            return context, self._active_page
        if pages:
            self._active_page = pages[-1]
            return context, self._active_page
        self._active_page = await context.new_page()
        return context, self._active_page

    async def _connect(self) -> None:
        from playwright.async_api import async_playwright

        cdp_url = self.config.cdp_url.rstrip("/")
        reachable = await self._probe_cdp(cdp_url)
        if not reachable:
            if not self.config.auto_launch:
                raise BrowserError(
                    f"无法连接到 Chrome 调试端口（{cdp_url}）。"
                    "请先带 --remote-debugging-port 启动 Chrome，或开启 tools.browser.auto_launch。"
                )
            await self._launch_chrome(cdp_url)

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                cdp_url,
                timeout=self._timeout_ms,
            )
        except Exception as exc:
            raise BrowserError(f"连接 Chrome CDP（{cdp_url}）失败：{exc}") from exc

        logger.info("tools.browser_connected", cdp_url=cdp_url)

    async def _probe_cdp(self, cdp_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
                resp = await client.get(f"{cdp_url}/json/version")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def _launch_chrome(self, cdp_url: str) -> None:
        port = parse_local_cdp_port(cdp_url)
        executable = find_chrome_executable(self.config.executable_path)
        if executable is None:
            raise BrowserError(
                "找不到 Google Chrome 可执行文件。"
                "请安装 Chrome 或在 config.yaml 的 tools.browser.executable_path 中指定路径。"
            )

        logger.info("tools.browser_launching", executable=str(executable), port=port)
        await asyncio.to_thread(
            _spawn_detached_chrome,
            executable=executable,
            port=port,
            user_data_dir=self.config.user_data_dir,
            base_cwd=self.base_cwd,
        )

        deadline = asyncio.get_running_loop().time() + 12.0
        while asyncio.get_running_loop().time() < deadline:
            if await self._probe_cdp(cdp_url):
                return
            await asyncio.sleep(0.25)
        raise BrowserError(f"已尝试启动 Chrome，但调试端口 {cdp_url} 在 12 秒内未就绪。")

    # ------------------------------------------------------------------ 动作实现

    async def open_url(self, url: str, *, new_tab: bool = True) -> str:
        async with self._lock:
            context, page = await self._ensure_page()
            if new_tab and page.url not in {"", "about:blank"}:
                page = await context.new_page()
                self._active_page = page

            await _safe_goto(page, url, timeout_ms=self._timeout_ms)
            await page.bring_to_front()
            index = _page_index(context, page)
            title = await _safe_title(page)
            logger.info("tools.browser_open", url=url, tab=index, title=title)
            return (
                f"已在标签页 [{index}] 打开：{title or '（无标题）'}\n"
                f"URL: {page.url}\n"
                "提示：可调用 browser_read_page 查看页面文本、媒体状态与可点击元素编号。"
            )

    async def list_tabs(self) -> str:
        async with self._lock:
            context, active = await self._ensure_page()
            pages = [p for p in context.pages if not p.is_closed()]
            lines = [f"当前共有 {len(pages)} 个标签页（* 为当前焦点）："]
            for idx, page in enumerate(pages):
                marker = "*" if page == active else " "
                title = await _safe_title(page)
                lines.append(f"{marker} [{idx}] {title or '（无标题）'} — {page.url}")
            return "\n".join(lines)

    async def switch_tab(self, index: int) -> str:
        async with self._lock:
            context, _ = await self._ensure_page()
            pages = [p for p in context.pages if not p.is_closed()]
            if index < 0 or index >= len(pages):
                raise BrowserError(f"标签页编号 {index} 越界，当前范围是 0–{len(pages) - 1}。")
            target = pages[index]
            self._active_page = target
            await target.bring_to_front()
            title = await _safe_title(target)
            logger.info("tools.browser_switch_tab", tab=index, url=target.url)
            return f"已切换到标签页 [{index}]：{title or '（无标题）'}\nURL: {target.url}"

    async def close_tab(self, index: int | None = None) -> str:
        async with self._lock:
            context, active = await self._ensure_page()
            pages = [p for p in context.pages if not p.is_closed()]
            if not pages:
                return "当前没有可关闭的标签页。"
            if index is None:
                target = active
                closed_idx = _page_index(context, target)
            else:
                if index < 0 or index >= len(pages):
                    raise BrowserError(f"标签页编号 {index} 越界，当前范围是 0–{len(pages) - 1}。")
                target = pages[index]
                closed_idx = index

            title = await _safe_title(target)
            url = target.url
            await target.close()

            remaining = [p for p in context.pages if not p.is_closed()]
            self._active_page = remaining[-1] if remaining else None
            logger.info("tools.browser_close_tab", tab=closed_idx, url=url)
            if self._active_page is not None:
                new_title = await _safe_title(self._active_page)
                new_idx = _page_index(context, self._active_page)
                return (
                    f"已关闭标签页 [{closed_idx}] {title or url}。"
                    f"当前焦点切换到 [{new_idx}] {new_title or self._active_page.url}。"
                )
            return f"已关闭最后一个标签页 [{closed_idx}] {title or url}。"

    async def read_page(self, *, filter_text: str = "", max_chars: int | None = None) -> str:
        async with self._lock:
            context, page = await self._ensure_page()
            limit = min(max_chars or self.config.max_read_chars, self.config.max_read_chars)
            snapshot = await page.evaluate(
                _READ_PAGE_JS,
                {"filterText": filter_text.strip(), "maxElements": _MAX_INTERACTIVE_ELEMENTS},
            )
            title = await _safe_title(page)
            index = _page_index(context, page)

            header = [f"标签页 [{index}] {title or '（无标题）'}", f"URL: {page.url}"]
            media = snapshot.get("media") or []
            if media:
                header.append("媒体播放器：" + "；".join(_format_media_item(m) for m in media))

            elements = snapshot.get("elements") or []
            if elements:
                header.append(f"\n可交互元素（共 {len(elements)} 项，用编号传给 click/type）：")
                for el in elements:
                    header.append(_format_element_line(el))
            elif filter_text.strip():
                header.append(f"\n未找到匹配 {filter_text!r} 的可交互元素。")

            body_text = str(snapshot.get("text") or "").strip()
            if body_text:
                header.append("\n页面正文摘要：\n" + body_text)

            result = truncate("\n".join(header), limit)
            logger.info(
                "tools.browser_read_page",
                tab=index,
                elements=len(elements),
                media=len(media),
                chars=len(result),
            )
            return result

    async def click(self, target: str) -> str:
        cleaned = _strip_ref_brackets(target)
        if not cleaned:
            raise BrowserError("点击目标不能为空，请传入元素编号、可见文本或 CSS 选择器。")

        async with self._lock:
            context, page = await self._ensure_page()
            pages_before = list(context.pages)
            locator = await _resolve_click_locator(page, cleaned)
            try:
                await locator.click(timeout=self._timeout_ms)
            except Exception as exc:
                raise BrowserError(
                    f"点击 {target!r} 失败：{exc}。建议先调用 browser_read_page 确认最新元素编号。"
                ) from exc

            switched = await _wait_and_follow_new_tab(
                context,
                page,
                pages_before,
                timeout_ms=self._timeout_ms,
            )
            self._active_page = switched
            title = await _safe_title(switched)
            idx = _page_index(context, switched)
            logger.info(
                "tools.browser_click", target=cleaned, tab=idx, new_tab=switched is not page
            )
            if switched is not page:
                return f"已点击 {target!r}，并自动切换到新标签页 [{idx}]：{title or switched.url}"
            return f"已点击 {target!r}。当前页面 [{idx}]：{title or '（无标题）'} — {switched.url}"

    async def type_text(self, target: str, text: str, *, submit: bool = True) -> str:
        cleaned = _strip_ref_brackets(target)
        if not cleaned:
            raise BrowserError("输入框目标不能为空，请传入元素编号、占位文字或 CSS 选择器。")

        async with self._lock:
            context, page = await self._ensure_page()
            pages_before = list(context.pages)
            locator = await _resolve_input_locator(page, cleaned)
            try:
                await locator.click(timeout=self._timeout_ms)
                await locator.fill(text, timeout=self._timeout_ms)
                if submit:
                    await locator.press("Enter", timeout=self._timeout_ms)
            except Exception as exc:
                raise BrowserError(f"向 {target!r} 输入内容失败：{exc}") from exc

            if submit:
                switched = await _wait_and_follow_new_tab(
                    context,
                    page,
                    pages_before,
                    timeout_ms=self._timeout_ms,
                )
                self._active_page = switched
            else:
                switched = page

            title = await _safe_title(switched)
            idx = _page_index(context, switched)
            logger.info("tools.browser_type", target=cleaned, submit=submit, tab=idx)
            action = "输入并提交" if submit else "输入"
            return f"已向 {target!r} {action}。当前页面 [{idx}]：{title or switched.url}"

    async def scroll(self, direction: str = "down", *, pages: float = 1.0) -> str:
        norm = direction.strip().lower()
        if norm not in SCROLL_DIRECTIONS:
            raise BrowserError(
                f"不支持的滚动方向 {direction!r}，请使用：{' / '.join(sorted(SCROLL_DIRECTIONS))}。"
            )
        async with self._lock:
            _, page = await self._ensure_page()
            await page.evaluate(_SCROLL_JS, {"direction": norm, "pages": max(0.1, float(pages))})
            logger.info("tools.browser_scroll", direction=norm, pages=pages)
            return f"已向 {norm} 滚动页面。"

    async def press_key(self, key: str) -> str:
        cleaned = key.strip()
        if not cleaned:
            raise BrowserError("按键名不能为空，例如 Space、Escape、Enter、f、ArrowRight。")
        async with self._lock:
            _, page = await self._ensure_page()
            await page.bring_to_front()
            await page.keyboard.press(cleaned)
            logger.info("tools.browser_press_key", key=cleaned)
            return f"已发送按键 {cleaned}。"

    async def media_control(self, action: str = "status", *, value: float | None = None) -> str:
        norm = action.strip().lower()
        if norm not in MEDIA_ACTIONS:
            raise BrowserError(
                f"不支持的媒体操作 {action!r}，可选：{' / '.join(sorted(MEDIA_ACTIONS))}。"
            )
        async with self._lock:
            context, page = await self._ensure_page()
            if norm in {"fullscreen", "exit_fullscreen"}:
                return await self._handle_fullscreen(context, page, enter=(norm == "fullscreen"))

            result = await page.evaluate(
                _MEDIA_CONTROL_JS,
                {"action": norm, "value": value},
            )
            if not result.get("found"):
                raise BrowserError(
                    "当前页面没有检测到 <video> 或 <audio> 播放器。"
                    "如果视频刚点击加载，请稍等或先切换到对应标签页。"
                )
            logger.info("tools.browser_media", action=norm, state=result.get("summary"))
            return str(result["summary"])

    async def window_state(self, state: str = "fullscreen") -> str:
        norm = state.strip().lower()
        if norm not in WINDOW_STATES:
            raise BrowserError(
                f"不支持的窗口状态 {state!r}，可选：{' / '.join(sorted(WINDOW_STATES))}。"
            )
        async with self._lock:
            context, page = await self._ensure_page()
            await page.bring_to_front()
            await _set_cdp_window_state(context, page, state=norm)
            logger.info("tools.browser_window", state=norm)
            labels = {
                "fullscreen": "已将 Chrome 窗口切换为全屏（F11 模式）。",
                "maximized": "已将 Chrome 窗口最大化。",
                "normal": "已将 Chrome 窗口恢复为普通大小。",
                "minimized": "已将 Chrome 窗口最小化。",
            }
            return labels[norm]

    async def _handle_fullscreen(self, context: BrowserContext, page: Page, *, enter: bool) -> str:
        await page.bring_to_front()
        if enter:
            # 1) 优先点击播放器原生的可见全屏按钮，保留弹幕/进度条等完整控件。
            if await _try_click_fullscreen_button(page):
                await asyncio.sleep(0.25)
                if await _is_dom_fullscreen(page):
                    return "已进入视频全屏播放。"

            # 2) 通过 CDP userGesture=True 执行 requestFullscreen，绕过无手势限制。
            res = await _cdp_eval_with_gesture(context, page, _ENTER_VIDEO_FULLSCREEN_JS)
            if isinstance(res, dict) and res.get("ok"):
                return str(res.get("message") or "已进入视频全屏播放。")

            # 3) 若页面无视频，默认最大化 Chrome 窗口（右上角最大化，保留标签栏与任务栏）；
            # 若有视频但网页全屏被拒绝，则切到窗口全屏。
            has_video = bool(isinstance(res, dict) and res.get("hasVideo"))
            if has_video:
                await _set_cdp_window_state(context, page, state="fullscreen")
                return "已将 Chrome 窗口切换为全屏播放。"
            await _set_cdp_window_state(context, page, state="maximized")
            return (
                "当前页面没有视频播放器，已将 Chrome 窗口最大化"
                "（保留标签栏与任务栏；若需覆盖整屏的 F11 全屏，"
                "请调用 browser_window(state='fullscreen')）。"
            )

        # 退出全屏：同时退出 DOM 全屏与 Chrome 窗口级全屏。
        exited_dom = False
        if await _is_dom_fullscreen(page):
            res = await _cdp_eval_with_gesture(context, page, _EXIT_DOM_FULLSCREEN_JS)
            exited_dom = bool(isinstance(res, dict) and res.get("ok"))

        win_state = await _get_cdp_window_state(context, page)
        exited_window = False
        if win_state == "fullscreen":
            await _set_cdp_window_state(context, page, state="normal")
            exited_window = True

        if exited_dom or exited_window:
            return "已退出全屏。"
        return "当前未处于全屏状态。"


# --------------------------------------------------------------------------- 工具构造


def build_tools(
    settings: Settings,
    *,
    backend: BrowserBackend | None = None,
) -> Sequence[BaseTool]:
    """构造 Chrome 浏览器控制工具；未启用或缺少 playwright 时返回空列表。"""
    reason = missing_requirement(settings)
    if reason:
        logger.debug("tools.browser_skipped", reason=reason)
        return []

    config = settings.app.tools.browser
    runtime: BrowserBackend = backend or PlaywrightBrowserBackend(config)
    tool_timeout = config.timeout + 15.0

    @tool
    async def browser_open(url: str, new_tab: bool = True) -> str:
        """在 Google Chrome 中打开指定网址。

        Args:
            url: 完整的 http:// 或 https:// 网址，例如 ``https://www.bilibili.com``。
            new_tab: 是否新建标签页打开（默认 True；若当前标签为空白页会直接复用）。

        Returns:
            打开后的标签页编号、页面标题与当前 URL。

        当用户要求打开网站、搜索页面或看视频时首先调用本工具。
        """
        safe_url = guard_navigation_url(url, allowed_domains=config.allowed_domains)
        return await runtime.open_url(safe_url, new_tab=new_tab)

    @tool
    async def browser_tabs(index: int | None = None) -> str:
        """查看当前打开的 Chrome 标签页列表，或切换到指定编号的标签页。

        Args:
            index: 留空时列出全部标签页；传入整数编号时切换到该标签页。

        Returns:
            标签页列表或切换后的当前标签页信息。
        """
        if index is None:
            return await runtime.list_tabs()
        return await runtime.switch_tab(index)

    @tool
    async def browser_close_tab(index: int | None = None) -> str:
        """关闭 Chrome 中的指定标签页（默认关闭当前焦点标签页）。

        Args:
            index: 要关闭的标签页编号；留空表示关闭当前焦点标签页。

        Returns:
            关闭结果以及剩余焦点标签页信息。
        """
        return await runtime.close_tab(index)

    @tool
    async def browser_read_page(filter_text: str = "") -> str:
        """读取当前 Chrome 标签页的标题、播放器状态、可交互元素编号与正文摘要。

        Args:
            filter_text: 可选过滤词。页面元素很多时传入关键词（如视频名、按钮文字），
                只列出包含该关键词的链接/按钮/输入框，大幅减少输出噪音。

        Returns:
            页面信息、播放器状态、带 ``[编号]`` 的可交互元素列表和正文文本。

        每次需要点击页面按钮、在网页搜索框输入或查看网页内容前调用。
        """
        return await runtime.read_page(filter_text=filter_text)

    @tool
    async def browser_click(target: str) -> str:
        """点击当前 Chrome 页面上的链接、按钮或元素。

        Args:
            target: 优先传 ``browser_read_page`` 返回的元素编号（如 ``"3"``），
                也可以传按钮/链接的可见文字（如 ``"搜索"``）或 CSS 选择器。

        Returns:
            点击后的当前页面信息；若点击打开了新标签页会自动切换过去并提示。
        """
        return await runtime.click(target)

    @tool
    async def browser_type(target: str, text: str, submit: bool = True) -> str:
        """在当前 Chrome 页面的输入框里填入文字，并可选择自动按回车提交。

        Args:
            target: ``browser_read_page`` 返回的输入框编号（如 ``"1"``）、
                占位文字或 CSS 选择器。
            text: 要输入的文本内容。
            submit: 填完后是否自动按 Enter 提交（默认 True，适合搜索框）。

        Returns:
            输入后的页面标题与 URL。
        """
        return await runtime.type_text(target, text, submit=submit)

    @tool
    async def browser_scroll(direction: str = "down", pages: float = 1.0) -> str:
        """上下滚动当前 Chrome 页面以查看更多内容。

        Args:
            direction: 滚动方向：``down`` / ``up`` / ``top`` / ``bottom``。
            pages: 每次滚动的视口屏数（默认 1.0）。

        Returns:
            滚动结果说明。
        """
        return await runtime.scroll(direction, pages=pages)

    @tool
    async def browser_press_key(key: str) -> str:
        """向当前 Chrome 页面发送键盘按键或快捷键。

        Args:
            key: Playwright 按键名，如 ``Space``、``Escape``、``Enter``、``f``、``ArrowRight``。

        Returns:
            按键发送结果。
        """
        return await runtime.press_key(key)

    @tool
    async def browser_media_control(action: str = "status", value: float | None = None) -> str:
        """控制当前网页里的视频或音频播放（支持播放、暂停、静音、音量、进度、倍速、全屏）。

        Args:
            action: 操作类型：
                ``status``（查看状态）/ ``play``（播放）/ ``pause``（暂停）/
                ``toggle``（切换播放暂停）/ ``mute`` / ``unmute`` /
                ``set_volume``（音量 0.0–1.0）/ ``seek``（跳到指定秒数）/
                ``forward``（快进，默认 15 秒）/ ``backward``（快退，默认 15 秒）/
                ``speed``（倍速，如 1.5）/ ``fullscreen`` / ``exit_fullscreen``。
            value: 可选数值参数（用于 ``set_volume`` / ``seek`` / ``forward`` /
                ``backward`` / ``speed``）。

        Returns:
            播放器当前状态摘要。

        控制视频播放或暂停时优先使用本工具，不需要先找页面上的暂停按钮。
        注意：``fullscreen`` 是视频播放器全屏；如果是让浏览器窗口最大化
        （点击右上角中间的方框按钮），请调用 ``browser_window(state="maximized")``。
        """
        return await runtime.media_control(action, value=value)

    @tool
    async def browser_window(state: str = "maximized") -> str:
        """调整当前 Chrome 浏览器窗口状态（最大化 / 还原 / 最小化 / F11 全屏）。

        Args:
            state: 目标窗口状态：
                ``maximized``（默认，窗口最大化，相当于点击浏览器右上角中间的方框按钮，
                保留标签栏、地址栏和系统任务栏；用户日常说「把浏览器全屏 / 放大窗口」优先用这个）/
                ``normal``（恢复普通大小窗口，取消最大化或退出 F11 全屏）/
                ``minimized``（最小化窗口）/
                ``fullscreen``（覆盖整个屏幕的 F11 全屏，隐藏标签栏与任务栏；
                仅在用户明确要求「覆盖整屏 / F11 无边框全屏」时使用）。

        Returns:
            窗口状态变更结果。

        当用户要求「把浏览器窗口最大化」「点击右上角最大化/全屏」或「还原窗口」时使用本工具。
        """
        return await runtime.window_state(state)

    annotate(browser_open, group=GROUP, side_effects=False, timeout=tool_timeout)
    annotate(browser_tabs, group=GROUP, side_effects=False, timeout=tool_timeout)
    annotate(browser_close_tab, group=GROUP, side_effects=True, timeout=tool_timeout)
    annotate(browser_read_page, group=GROUP, side_effects=False, timeout=tool_timeout)
    annotate(browser_click, group=GROUP, side_effects=True, timeout=tool_timeout)
    annotate(browser_type, group=GROUP, side_effects=True, timeout=tool_timeout)
    annotate(browser_scroll, group=GROUP, side_effects=False, timeout=tool_timeout)
    annotate(browser_press_key, group=GROUP, side_effects=True, timeout=tool_timeout)
    annotate(browser_media_control, group=GROUP, side_effects=False, timeout=tool_timeout)
    annotate(browser_window, group=GROUP, side_effects=False, timeout=tool_timeout)

    return [
        browser_open,
        browser_tabs,
        browser_close_tab,
        browser_read_page,
        browser_click,
        browser_type,
        browser_scroll,
        browser_press_key,
        browser_media_control,
        browser_window,
    ]


# --------------------------------------------------------------------------- Playwright 内部辅助


def _spawn_detached_chrome(
    *,
    executable: Path,
    port: int,
    user_data_dir: str,
    base_cwd: Path,
) -> None:
    """在独立进程组拉起 Chrome，确保豆豆退出后浏览器窗口仍保留。"""
    raw_profile = Path(user_data_dir).expanduser()
    profile_dir = (raw_profile if raw_profile.is_absolute() else (base_cwd / raw_profile)).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    argv = build_chrome_launch_args(executable, port=port, user_data_dir=profile_dir)
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    # 已校验的本机可执行文件路径与固定参数列表，不经过 shell。
    subprocess.Popen(argv, **popen_kwargs)  # noqa: S603


def _strip_ref_brackets(target: str) -> str:
    cleaned = target.strip()
    if cleaned.startswith("[") and cleaned.endswith("]") and cleaned[1:-1].isdigit():
        return cleaned[1:-1]
    return cleaned


def _page_index(context: BrowserContext, page: Page) -> int:
    pages = [p for p in context.pages if not p.is_closed()]
    try:
        return pages.index(page)
    except ValueError:
        return max(0, len(pages) - 1)


async def _safe_title(page: Page) -> str:
    try:
        return (await page.title()).strip()
    except Exception:
        return ""


async def _safe_goto(page: Page, url: str, *, timeout_ms: float) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        raise BrowserError(f"打开 {url} 失败：{exc}") from exc


async def _wait_and_follow_new_tab(
    context: BrowserContext,
    current: Page,
    pages_before: list[Page],
    *,
    timeout_ms: float,
) -> Page:
    await asyncio.sleep(0.6)
    new_pages = [p for p in context.pages if p not in pages_before and not p.is_closed()]
    if new_pages:
        newest = new_pages[-1]
        try:
            await newest.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 8000.0))
        except Exception as exc:
            logger.debug("tools.browser_new_tab_load_wait", url=newest.url, error=str(exc))
        await newest.bring_to_front()
        return newest
    try:
        await current.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 5000.0))
    except Exception as exc:
        logger.debug("tools.browser_tab_load_wait", url=current.url, error=str(exc))
    return current


async def _resolve_click_locator(page: Page, target: str) -> Locator:
    if target.isdigit():
        locator = page.locator(f'[data-doudou-ref="{target}"]').first
        if await locator.count() > 0:
            return locator
        raise BrowserError(
            f"页面上没有找到编号 [{target}] 的元素，请先调用 browser_read_page 获取最新编号。"
        )

    if target.startswith(("#", ".", "[")) or ">" in target:
        css = page.locator(target).first
        if await css.count() > 0:
            return css

    for role in ("button", "link"):
        candidate = page.get_by_role(role, name=target).first
        if await candidate.count() > 0:
            return candidate

    text_match = page.get_by_text(target, exact=False).first
    if await text_match.count() > 0:
        return text_match

    return page.locator(target).first


async def _resolve_input_locator(page: Page, target: str) -> Locator:
    if target.isdigit():
        locator = page.locator(f'[data-doudou-ref="{target}"]').first
        if await locator.count() > 0:
            return locator
        raise BrowserError(
            f"页面上没有找到编号 [{target}] 的输入框，请先调用 browser_read_page 获取最新编号。"
        )

    for candidate in (
        page.get_by_placeholder(target, exact=False).first,
        page.get_by_label(target, exact=False).first,
        page.locator(target).first,
    ):
        if await candidate.count() > 0:
            return candidate
    return page.locator("input, textarea").first


async def _try_click_fullscreen_button(page: Page) -> bool:
    for selector in _FULLSCREEN_BUTTON_SELECTORS:
        locator = page.locator(selector).first
        try:
            if await locator.count() > 0 and await locator.is_visible():
                await locator.click(timeout=2000.0)
                return True
        except Exception as exc:
            logger.debug("tools.browser_fullscreen_btn_skip", selector=selector, error=str(exc))
    return False


async def _is_dom_fullscreen(page: Page) -> bool:
    try:
        return bool(await page.evaluate("() => Boolean(document.fullscreenElement)"))
    except Exception:
        return False


async def _cdp_eval_with_gesture(context: BrowserContext, page: Page, expression: str) -> Any:
    cdp = await context.new_cdp_session(page)
    try:
        resp = await cdp.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "userGesture": True,
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        return resp.get("result", {}).get("value")
    except Exception as exc:
        logger.debug("tools.browser_cdp_eval_failed", error=str(exc))
        return None
    finally:
        await cdp.detach()


async def _get_cdp_window_state(context: BrowserContext, page: Page) -> str | None:
    cdp = await context.new_cdp_session(page)
    try:
        info = await cdp.send("Browser.getWindowForTarget")
        bounds = info.get("bounds") or {}
        state = bounds.get("windowState")
        return str(state) if state is not None else None
    except Exception as exc:
        logger.debug("tools.browser_get_window_failed", error=str(exc))
        return None
    finally:
        await cdp.detach()


async def _set_cdp_window_state(context: BrowserContext, page: Page, *, state: str) -> None:
    cdp = await context.new_cdp_session(page)
    try:
        info = await cdp.send("Browser.getWindowForTarget")
        window_id = info["windowId"]
        current = str((info.get("bounds") or {}).get("windowState") or "")
        if current == state:
            return
        # Chrome CDP 要求先从 minimized/fullscreen 恢复到 normal，才能再切换到目标状态。
        if current in {"minimized", "fullscreen"} and state != "normal":
            await cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "normal"}},
            )
        await cdp.send(
            "Browser.setWindowBounds",
            {"windowId": window_id, "bounds": {"windowState": state}},
        )
    except Exception as exc:
        raise BrowserError(f"设置 Chrome 窗口状态（{state}）失败：{exc}") from exc
    finally:
        await cdp.detach()


def _format_element_line(el: dict[str, Any]) -> str:
    ref = el.get("ref")
    tag = el.get("tag", "")
    text = str(el.get("text") or "").strip()
    placeholder = str(el.get("placeholder") or "").strip()
    href = str(el.get("href") or "").strip()
    parts = [f"[{ref}] <{tag}>"]
    if text:
        parts.append(f'"{truncate(text, 80)}"')
    if placeholder:
        parts.append(f'placeholder="{truncate(placeholder, 40)}"')
    if href:
        parts.append(f"-> {truncate(href, 90)}")
    return " ".join(parts)


def _format_media_item(item: dict[str, Any]) -> str:
    state = "⏸ 已暂停" if item.get("paused") else "▶ 播放中"
    cur = _format_seconds(item.get("currentTime"))
    dur = _format_seconds(item.get("duration"))
    vol = round(float(item.get("volume", 1.0)) * 100)
    muted = "（已静音）" if item.get("muted") else ""
    return f"{state} {cur}/{dur} 音量 {vol}%{muted}"


def _format_seconds(raw: Any) -> str:
    if not isinstance(raw, (int, float)) or raw < 0 or raw != raw:  # NaN check
        return "00:00"
    total = int(raw)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


# --------------------------------------------------------------------------- 页面 JS 片段

_READ_PAGE_JS = """
({ filterText, maxElements }) => {
  document.querySelectorAll('[data-doudou-ref]').forEach((el) => {
    el.removeAttribute('data-doudou-ref');
  });

  const query = (filterText || '').toLowerCase();
  const selector = [
    'a[href]',
    'button',
    'input',
    'textarea',
    'select',
    '[role="button"]',
    '[role="link"]',
    '[role="textbox"]',
    '[role="searchbox"]',
  ].join(', ');
  const nodes = Array.from(document.querySelectorAll(selector));
  const elements = [];
  let ref = 0;

  for (const el of nodes) {
    if (elements.length >= maxElements) break;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
      continue;
    }
    const rect = el.getBoundingClientRect();
    if (rect.width <= 1 || rect.height <= 1) continue;

    const tag = el.tagName.toLowerCase();
    const label = (
      el.innerText
      || el.getAttribute('aria-label')
      || el.getAttribute('title')
      || el.value
      || ''
    );
    const rawText = label.replace(/\\s+/g, ' ').trim();
    const placeholder = (el.getAttribute('placeholder') || '').trim();
    const href = tag === 'a' ? (el.href || '') : '';

    if (tag === 'a' && !rawText && !href) continue;
    if (query) {
      const hay = `${rawText} ${placeholder} ${href}`.toLowerCase();
      if (!hay.includes(query)) continue;
    }

    ref += 1;
    el.setAttribute('data-doudou-ref', String(ref));
    elements.push({ ref, tag, text: rawText, placeholder, href });
  }

  const media = Array.from(document.querySelectorAll('video, audio')).map((m) => ({
    tag: m.tagName.toLowerCase(),
    paused: m.paused,
    ended: m.ended,
    muted: m.muted,
    volume: m.volume,
    currentTime: Number.isFinite(m.currentTime) ? m.currentTime : 0,
    duration: Number.isFinite(m.duration) ? m.duration : 0,
  }));

  const rawBody = (document.body && document.body.innerText) ? document.body.innerText : '';
  const text = rawBody.split('\\n').map((l) => l.trim()).filter(Boolean).join('\\n');
  return { elements, media, text };
}
"""

_SCROLL_JS = """
({ direction, pages }) => {
  const step = Math.round(window.innerHeight * 0.85 * pages);
  if (direction === 'top') {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } else if (direction === 'bottom') {
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
  } else if (direction === 'up') {
    window.scrollBy({ top: -step, behavior: 'smooth' });
  } else {
    window.scrollBy({ top: step, behavior: 'smooth' });
  }
}
"""

_MEDIA_CONTROL_JS = """
async ({ action, value }) => {
  const items = Array.from(document.querySelectorAll('video, audio'));
  if (!items.length) return { found: false };
  const target = items.find((m) => m.tagName.toLowerCase() === 'video' && !m.paused)
    || items.find((m) => m.tagName.toLowerCase() === 'video')
    || items[0];

  if (action === 'play') {
    try { await target.play(); } catch (_) {}
  } else if (action === 'pause') {
    target.pause();
  } else if (action === 'toggle') {
    if (target.paused) {
      try { await target.play(); } catch (_) {}
    } else {
      target.pause();
    }
  } else if (action === 'mute') {
    target.muted = true;
  } else if (action === 'unmute') {
    target.muted = false;
  } else if (action === 'set_volume' && value !== null) {
    target.volume = Math.max(0, Math.min(1, Number(value)));
    if (target.volume > 0) target.muted = false;
  } else if (action === 'seek' && value !== null) {
    target.currentTime = Math.max(0, Number(value));
  } else if (action === 'forward') {
    const delta = value !== null ? Number(value) : 15;
    target.currentTime = Math.max(0, (target.currentTime || 0) + delta);
  } else if (action === 'backward') {
    const delta = value !== null ? Number(value) : 15;
    target.currentTime = Math.max(0, (target.currentTime || 0) - delta);
  } else if (action === 'speed' && value !== null) {
    target.playbackRate = Math.max(0.25, Math.min(4.0, Number(value)));
  }

  const fmt = (sec) => {
    if (!Number.isFinite(sec) || sec < 0) return '00:00';
    const t = Math.floor(sec);
    const m = Math.floor(t / 60);
    const s = t % 60;
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  };
  const status = target.paused ? '⏸ 已暂停' : '▶ 正在播放';
  const vol = Math.round((target.volume || 0) * 100);
  const muted = target.muted ? '（已静音）' : '';
  const speed = target.playbackRate && target.playbackRate !== 1 ? ` ${target.playbackRate}x` : '';
  const timeRange = `${fmt(target.currentTime)} / ${fmt(target.duration)}`;
  return {
    found: true,
    summary: `${status} ${timeRange} | 音量 ${vol}%${muted}${speed}`,
  };
}
"""

_ENTER_VIDEO_FULLSCREEN_JS = """
(async () => {
  const video = document.querySelector('video');
  if (!video) return { ok: false, hasVideo: false, message: '' };
  const container = video.closest(
    '.bpx-player-container, .bilibili-player-video-wrap, .html5-video-player, [class*="player"]'
  ) || video;
  try {
    if (!document.fullscreenElement) {
      await container.requestFullscreen();
    }
    if (document.fullscreenElement) {
      return { ok: true, hasVideo: true, message: '已进入视频全屏播放。' };
    }
  } catch (_) {}
  try {
    await video.requestFullscreen();
    if (document.fullscreenElement) {
      return { ok: true, hasVideo: true, message: '已进入视频全屏播放。' };
    }
  } catch (_) {}
  return { ok: false, hasVideo: true, message: '' };
})()
"""

_EXIT_DOM_FULLSCREEN_JS = """
(async () => {
  if (!document.fullscreenElement) return { ok: false };
  try {
    await document.exitFullscreen();
    return { ok: !document.fullscreenElement };
  } catch (_) {
    return { ok: false };
  }
})()
"""

__all__ = [
    "GROUP",
    "MEDIA_ACTIONS",
    "SCROLL_DIRECTIONS",
    "WINDOW_STATES",
    "BrowserBackend",
    "BrowserError",
    "PlaywrightBrowserBackend",
    "build_chrome_launch_args",
    "build_tools",
    "find_chrome_executable",
    "guard_navigation_url",
    "missing_requirement",
    "parse_local_cdp_port",
    "playwright_available",
]
