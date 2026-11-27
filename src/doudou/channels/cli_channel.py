"""Interactive terminal channel for local development."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.messages import BaseMessage
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.prompt import Confirm
from rich.status import Status
from rich.table import Table

from doudou.agent.events import RunEvent
from doudou.agent.react import ApprovalHook, RunResult, ToolApprovalRequest
from doudou.channels.base import InboundMessage, OutboundMessage
from doudou.gateway.history import ThreadInfo
from doudou.gateway.runner import RunnerLike
from doudou.gateway.schema import RunRequest
from doudou.observability.logging import get_logger
from doudou.utils.text import content_to_text, truncate

logger = get_logger(__name__)

DEFAULT_CONVERSATION = "local"
MAX_TOOL_RESULT_CHARS = 300

_HELP = """\
[b]可用命令[/b]
  /new           开启新会话（换新的 thread，清空上下文）
  /threads       查看历史会话并选择继续对话
  /tools         列出已装载的工具
  /help          显示本帮助
  /exit          退出

直接输入内容即向助手提问。"""


@dataclass(slots=True)
class _RenderState:
    """渲染过程中的临时状态。"""

    text_started: bool = False
    buffer: str = ""
    live: Live | None = None
    status: Status | None = None
    pending_tools: int = 0
    rendered_any: bool = False
    last_refresh: float = 0.0

    def reset(self) -> None:
        self.text_started = False
        self.buffer = ""
        self.live = None
        self.status = None
        self.pending_tools = 0
        self.rendered_any = False
        self.last_refresh = 0.0


#: 流式 ``Live`` 刷新的最小间隔（秒），兼顾跟手度与大代码块重绘开销。
_LIVE_REFRESH_INTERVAL = 0.04


class CliChannel:
    """终端通道：读入用户输入，把 ReAct 事件渲染成人看的输出。

    它实现了 ``Channel`` 协议（与其他通道并列启停），
    同时直接被 ``doudou chat`` / ``doudou ask`` 使用。
    """

    name = "cli"

    def __init__(
        self,
        runner: RunnerLike,
        *,
        stream: bool = True,
        echo_text: bool = True,
        console: Console | None = None,
        show_tool_calls: bool = True,
        max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
        render_markdown: bool = True,
    ) -> None:
        self.runner = runner
        self.stream = stream
        #: 是否把模型输出的增量正文渲染到 ``console``。
        #: ``False`` 时只聚合不渲染，由调用方决定怎么输出最终答复（``--quiet``）。
        self.echo_text = echo_text
        self.console = console or Console()
        self.show_tool_calls = show_tool_calls
        self.max_tool_result_chars = max_tool_result_chars
        self.render_markdown = render_markdown
        self._state = _RenderState()
        self._active_threads: dict[str, str] = {}

    # ---------------------------------------------------------------- Channel 协议

    async def start(self) -> None:
        """无外部资源需要建立。"""
        return None

    async def stop(self) -> None:
        """无外部资源需要释放。"""
        return None

    async def send(self, message: OutboundMessage) -> None:
        """非流式地输出一条消息。"""
        self.console.print(message.text, markup=False, highlight=False)

    # ---------------------------------------------------------------- 入口

    def select_thread(self, conversation_id: str, thread_id: str) -> None:
        """指定某个终端会话标识接下来要继续使用的 ``thread_id``。"""
        self._active_threads[conversation_id] = thread_id

    async def show_thread_preview(self, thread_id: str) -> None:
        """展示选定历史会话的最近几轮对话摘要。"""
        messages = await self.runner.history(thread_id)
        self.console.print(f"[green]已切换到会话[/green] [dim]{thread_id}[/dim]")
        print_thread_preview(self.console, messages)

    async def ask(self, text: str, *, conversation_id: str = DEFAULT_CONVERSATION) -> RunResult:
        """跑一轮对话并渲染结果。"""
        request = RunRequest(
            text=text,
            conversation_id=conversation_id,
            channel=self.name,
            thread_id=self._active_threads.get(conversation_id),
        )
        thread_id = await self.runner.resolve_thread(request)
        self._state.reset()

        if self.stream:
            result = await self._consume(
                self.runner.astream(request, thread_id=thread_id), thread_id=thread_id
            )
        else:
            result = await self.runner.ainvoke(request, thread_id=thread_id)

        resolved_id = next(
            (ev.thread_id for ev in reversed(result.events) if ev.thread_id),
            thread_id,
        )
        if resolved_id:
            self._active_threads[conversation_id] = resolved_id
        self._finish(result)
        return result

    async def repl(self, *, conversation_id: str = DEFAULT_CONVERSATION) -> None:
        """交互式多轮对话循环。"""
        self._print_banner()

        while True:
            try:
                raw = await asyncio.to_thread(self.console.input, "[bold cyan]你 ›[/] ")
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                break

            text = raw.strip()
            if not text:
                continue
            if text.startswith("/"):
                if not await self._handle_command(text, conversation_id=conversation_id):
                    break
                continue

            result = await self.ask(text, conversation_id=conversation_id)
            if result.ok:
                self.console.print(f"[dim]（{result.steps} 步，{_usage_text(result.usage)}）[/dim]")
            self.console.print()

        self.console.print("[dim]再见。[/dim]")

    # ---------------------------------------------------------------- 渲染

    async def deliver(self, message: InboundMessage, events: AsyncIterator[RunEvent]) -> None:
        """实现 ``EventSink``：把事件流渲染到终端。

        与 :meth:`ask` 的区别是它**不自己发起运行** —— 请求已经由
        ``gateway.MessageRouter`` 组装好，这里只负责消费与渲染。
        """
        request = RunRequest(
            text=message.text,
            conversation_id=message.session_key,
            channel=self.name,
            user_id=message.user_id,
        )
        thread_id = await self.runner.resolve_thread(request)
        self._state.reset()
        result = await self._consume(events, thread_id=thread_id)
        self._finish(result)

    async def _consume(self, stream: AsyncIterator[RunEvent], *, thread_id: str) -> RunResult:
        """消费事件流：边渲染边聚合，返回与 ``ainvoke`` 等价的结果。"""
        events: list[RunEvent] = []
        text = ""
        steps = 0
        usage: dict[str, int] = {}
        error: str | None = None

        self._start_thinking()
        try:
            async for event in stream:
                events.append(event)
                self._render(event)
                if event.type == "run_end":
                    text = event.text
                    steps = event.step
                    usage = event.usage
                elif event.type == "error":
                    error = event.text
                    steps = event.step
        finally:
            # 确保异常或中断退出时也会停掉 Status 与 Live，恢复终端光标。
            self._stop_thinking()
            self._stop_live()

        return RunResult(
            text=text,
            steps=steps,
            messages=await self.runner.history(thread_id),
            usage=usage,
            events=events,
            ok=error is None,
            error=error,
        )

    def _should_render_markdown(self) -> bool:
        """仅在启用渲染、输出正文且处于交互式终端时启用 Markdown。"""
        return self.render_markdown and self.echo_text and self.console.is_terminal

    def _start_thinking(self, label: str = "豆豆正在思考…") -> None:
        """在交互终端等待模型响应期间展示轻量等待状态，收到任何实际输出前停掉。"""
        if not (self.echo_text and self.console.is_terminal):
            return
        if self._state.live is not None or self._state.text_started or self._state.buffer:
            return
        if self._state.status is None:
            status = self.console.status(f"[dim]{label}[/dim]", spinner="dots")
            status.start()
            self._state.status = status
        else:
            self._state.status.update(f"[dim]{label}[/dim]")

    def _stop_thinking(self) -> None:
        if self._state.status is not None:
            try:
                self._state.status.stop()
            finally:
                self._state.status = None

    def _stop_live(self) -> None:
        if self._state.live is not None:
            try:
                self._state.live.stop()
            finally:
                self._state.live = None
                self._state.last_refresh = 0.0

    def _flush_markdown(self) -> None:
        """停掉动态预览，把缓冲里剩余文本以 Markdown 固化打印。"""
        self._stop_live()
        remaining = self._state.buffer
        self._state.buffer = ""
        if remaining.strip():
            self.console.print(Markdown(remaining))
            self._state.rendered_any = True
        self._state.text_started = False

    def _stream_markdown_delta(self, delta: str) -> None:
        self._state.text_started = True
        self._state.buffer += delta

        committed, tail = _split_completed_blocks(self._state.buffer)
        if committed:
            self._stop_live()
            if committed.strip():
                self.console.print(Markdown(committed))
                self._state.rendered_any = True
            self._state.buffer = tail

        if not self._state.buffer.strip():
            return

        now = time.monotonic()
        if self._state.live is None:
            self._state.live = Live(
                Markdown(self._state.buffer),
                console=self.console,
                transient=True,
                vertical_overflow="visible",
                auto_refresh=False,
            )
            self._state.live.start()
            self._state.last_refresh = now
        elif now - self._state.last_refresh >= _LIVE_REFRESH_INTERVAL:
            self._state.live.update(Markdown(self._state.buffer), refresh=True)
            self._state.last_refresh = now

    def _render(self, event: RunEvent) -> None:
        if event.type == "text_delta":
            if not self.echo_text or not event.text:
                return
            self._stop_thinking()
            if self._should_render_markdown():
                self._stream_markdown_delta(event.text)
            else:
                self._state.text_started = True
                self.console.print(event.text, end="", markup=False, highlight=False)
            return

        if event.type == "tool_call":
            # 工具执行（含 Confirm.ask）前先收口等待提示与流式输出，避免穿插。
            self._state.pending_tools += 1
            self._stop_thinking()
            self._break_line()
            if self.show_tool_calls:
                args = _format_args(event.tool_args or {})
                self.console.print(f"[dim]· 调用工具 [b]{event.tool_name}[/b]{args}[/dim]")
            return

        if event.type == "tool_result":
            self._state.pending_tools = max(0, self._state.pending_tools - 1)
            if self.show_tool_calls:
                marker = "←" if event.tool_ok else "✗"
                body = truncate(event.result or "", self.max_tool_result_chars)
                style = "dim" if event.tool_ok else "bold red"
                self.console.print(f"[{style}]  {marker} {body}[/]")
            if self._state.pending_tools == 0:
                self._start_thinking("豆豆正在整理…")
            return

        if event.type == "error":
            self._stop_thinking()
            self._break_line()
            self.console.print(f"[bold red]✗ {event.text}[/]")
            return

        if event.type == "run_end":
            self._stop_thinking()
            # AgentRunner.astream 在 yield run_end 之后、退出前会在 finally 打
            # runner.run_finished 日志；必须先停掉 Live 并固化正文，避免 stderr
            # 日志打断 Live 光标回退导致正文重复渲染。
            if self._should_render_markdown():
                self._flush_markdown()
            elif self._state.text_started:
                self.console.print()
                self._state.text_started = False
                self._state.rendered_any = True

    def _finish(self, result: RunResult) -> None:
        if not self.echo_text:
            return
        if self._should_render_markdown():
            self._flush_markdown()
            if not self._state.rendered_any and result.text:
                self.console.print(Markdown(result.text))
            return
        if self._state.text_started:
            self.console.print()
        elif result.text and not self._state.rendered_any:
            # 非流式（或模型一次性返回）时补上正文。
            self.console.print(result.text, markup=False, highlight=False)

    def _break_line(self) -> None:
        """如果已经输出过正文，先收口当前段，避免工具日志或确认提示与正文粘在一起。"""
        if self._should_render_markdown():
            if self._state.live is not None or self._state.buffer or self._state.text_started:
                self._flush_markdown()
            return
        if self._state.text_started:
            self.console.print()
            self._state.text_started = False

    # ---------------------------------------------------------------- REPL 命令

    def _print_banner(self) -> None:
        self.console.print(
            f"[bold]{self.runner.settings.app.agent.name}[/bold] "
            f"[dim]· 模型 {self.runner.model_label} · {self.runner.tool_count} 个工具[/dim]"
        )
        self.console.print("[dim]输入 /help 查看命令，/exit 退出。[/dim]\n")

    async def _handle_command(self, command: str, *, conversation_id: str) -> bool:
        """处理斜杠命令；返回 ``False`` 表示应退出 REPL。

        是异步的：``/new``、``/threads`` 与 ``/tools`` 需要异步调用运行器。
        """
        parts = command.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if name in {"/exit", "/quit"}:
            return False
        if name == "/help":
            self.console.print(_HELP)
            return True
        if name == "/tools":
            await self._print_tools()
            return True
        if name == "/new":
            self._active_threads.pop(conversation_id, None)
            thread_id = await self.runner.start_new_thread(conversation_id, channel=self.name)
            self.console.print(f"[green]已开启新会话[/green] [dim]{thread_id}[/dim]")
            return True
        if name == "/threads":
            await self._select_thread_command(conversation_id=conversation_id, selector=arg)
            return True

        self.console.print(f"[yellow]未知命令：{name}（/help 查看可用命令）[/yellow]")
        return True

    async def _select_thread_command(self, *, conversation_id: str, selector: str) -> None:
        threads = await self.runner.list_threads(limit=20)
        if not threads:
            self.console.print("[dim]暂无历史会话。[/dim]")
            return

        current_id = self._active_threads.get(conversation_id)
        if not selector:
            render_threads_table(
                self.console,
                threads,
                current_thread_id=current_id,
                timezone_name=self.runner.settings.app.runtime.timezone,
            )
            try:
                selector = (
                    await asyncio.to_thread(
                        self.console.input,
                        "[dim]输入编号或会话 ID 继续对话（直接回车取消）：[/] ",
                    )
                ).strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                return
            if not selector:
                return

        selected = resolve_thread_choice(threads, selector)
        if selected is None:
            self.console.print(f"[yellow]未找到匹配的历史会话：{selector}[/yellow]")
            return
        self.select_thread(conversation_id, selected.thread_id)
        await self.show_thread_preview(selected.thread_id)

    async def _print_tools(self) -> None:
        table = Table(title="已装载的工具")
        table.add_column("名称", style="bold")
        table.add_column("分组", style="dim")
        table.add_column("副作用", justify="center")
        table.add_column("需确认", justify="center")
        table.add_column("说明")
        for info in await self.runner.list_tool_infos():
            table.add_row(
                info.name,
                info.group,
                "是" if info.side_effects else "—",
                "是" if info.requires_approval else "—",
                description_summary(info.description),
            )
        self.console.print(table)


def make_approval_hook(console: Console) -> ApprovalHook:
    """构造「在终端里询问是否执行工具」的确认回调。

    是模块级函数而非 :class:`CliChannel` 的方法，因为它需要在
    ``AgentRunner`` 之前构造（``ReactAgent`` 在创建时就固定了回调）。
    """

    async def _confirm(request: ToolApprovalRequest) -> bool:
        args = _format_args(request.args)
        prompt = f"是否执行工具 {request.tool_name}{args}？（第 {request.step} 步）"
        try:
            return await asyncio.to_thread(Confirm.ask, prompt, console=console)
        except (EOFError, KeyboardInterrupt):
            console.print("[yellow]已取消确认，视为拒绝。[/yellow]")
            return False

    return _confirm


def make_auto_approve_hook(console: Console) -> ApprovalHook:
    """构造「一律同意」的确认回调，对应 ``--no-confirm``。

    ``--no-confirm`` 的字面意思是「别问我」，所以默认执行而不是拒绝 ——
    否则这个开关等于把有副作用的工具全禁掉，没有任何用处。
    代价是它同时也放开了破坏性操作，因此每次都打一条日志留痕。
    """

    async def _approve(request: ToolApprovalRequest) -> bool:
        logger.info(
            "cli.tool_auto_approved",
            tool=request.tool_name,
            step=request.step,
            call_id=request.call_id,
            args=request.args,
        )
        console.print(
            f"[yellow]· 已按 --no-confirm 自动同意执行 {request.tool_name}"
            f"{_format_args(request.args)}[/yellow]"
        )
        return True

    return _approve


def _fence_marker(stripped: str) -> tuple[str, int] | None:
    """识别 CommonMark 围栏代码块起始/结束行，返回 ``(字符, 长度)``。"""
    if not stripped or stripped[0] not in ("`", "~"):
        return None
    char = stripped[0]
    length = 0
    while length < len(stripped) and stripped[length] == char:
        length += 1
    if length < 3:
        return None
    return char, length


def _split_completed_blocks(text: str) -> tuple[str, str]:
    """切出已完结、可安全固化打印的 Markdown 顶层块，保留末尾仍在流式输出的块。

    只在不处于围栏代码块内部、且遇到空行时切分，避免把代码块、列表或表格从中间截断。
    """
    lines = text.splitlines(keepends=True)
    fence_char: str | None = None
    fence_len = 0
    seen_content = False
    split_pos = 0
    pos = 0

    for line in lines:
        pos += len(line)
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        marker = _fence_marker(stripped) if indent <= 3 else None
        if marker is not None:
            char, length = marker
            rest = stripped[length:]
            if fence_char is None:
                # CommonMark 规则：反引号围栏的 info string 不能包含反引号（避免误判行内代码）。
                if char == "`" and "`" in rest:
                    seen_content = True
                    continue
                fence_char = char
                fence_len = length
                seen_content = True
                continue
            if char == fence_char and length >= fence_len and rest.strip() == "":
                fence_char = None
                fence_len = 0
                continue

        if fence_char is not None:
            continue

        if line.strip() == "":
            if seen_content:
                split_pos = pos
        else:
            seen_content = True

    if split_pos == 0:
        return "", text
    return text[:split_pos], text[split_pos:]


def description_summary(text: str, limit: int = 44) -> str:
    """取说明首行并截断，供终端表格显示。"""
    stripped = text.strip()
    if not stripped:
        return ""
    return truncate(stripped.splitlines()[0].strip(), limit)


def _format_args(args: dict[str, Any]) -> str:
    if not args:
        return ""
    rendered = ", ".join(f"{key}={value!r}" for key, value in args.items())
    return f"({truncate(rendered, 120)})"


def _format_thread_time(dt: datetime | None, timezone_name: str) -> str:
    if dt is None:
        return "—"
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return f"{dt.astimezone(tz):%Y-%m-%d %H:%M}"


def render_threads_table(
    console: Console,
    threads: Sequence[ThreadInfo],
    *,
    current_thread_id: str | None = None,
    timezone_name: str = "UTC",
) -> None:
    """以表格列出最近的历史会话。"""
    table = Table(title="历史会话")
    table.add_column("编号", justify="right", style="cyan")
    table.add_column("会话 ID", style="bold")
    table.add_column("渠道", style="dim")
    table.add_column("消息数", justify="right")
    table.add_column("更新时间", style="dim")
    for idx, item in enumerate(threads, start=1):
        is_current = bool(current_thread_id and item.thread_id == current_thread_id)
        marker = f"[green]* {idx}[/green]" if is_current else str(idx)
        table.add_row(
            marker,
            item.thread_id,
            item.channel or "—",
            str(item.message_count),
            _format_thread_time(item.updated_at, timezone_name),
        )
    console.print(table)


def resolve_thread_choice(threads: Sequence[ThreadInfo], choice: str) -> ThreadInfo | None:
    """根据用户输入的 1-based 编号或 thread_id（含唯一前缀）解析目标会话。"""
    raw = choice.strip()
    if not raw:
        return None
    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(threads):
            return threads[index - 1]
        return None
    for item in threads:
        if item.thread_id == raw:
            return item
    if len(raw) >= 4:
        matches = [item for item in threads if item.thread_id.startswith(raw)]
        if len(matches) == 1:
            return matches[0]
    return None


def print_thread_preview(
    console: Console, messages: Sequence[BaseMessage], *, max_items: int = 4
) -> None:
    """打印所选会话最近几轮的用户/助手对话预览。"""
    turns: list[tuple[str, str]] = []
    for msg in messages:
        if msg.type not in {"human", "ai"}:
            continue
        text = " ".join(content_to_text(msg.content).split())
        if not text:
            continue
        label = "你" if msg.type == "human" else "助手"
        turns.append((label, truncate(text, 80)))
    if not turns:
        console.print("[dim]（该会话暂无历史对话内容）[/dim]")
        return
    for label, preview in turns[-max_items:]:
        console.print(f"[dim]  {label}：{preview}[/dim]")


def _usage_text(usage: dict[str, int]) -> str:
    parts = [f"{key}={value}" for key, value in usage.items() if key.endswith("_tokens")]
    return "、".join(parts) if parts else "无用量统计"


__all__ = [
    "DEFAULT_CONVERSATION",
    "CliChannel",
    "description_summary",
    "make_approval_hook",
    "make_auto_approve_hook",
    "print_thread_preview",
    "render_threads_table",
    "resolve_thread_choice",
]
