"""Interactive terminal channel for local development."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from doudou.agent.events import RunEvent
from doudou.agent.react import ApprovalHook, RunResult, ToolApprovalRequest
from doudou.channels.base import InboundMessage, OutboundMessage
from doudou.gateway.runner import AgentRunner
from doudou.gateway.schema import RunRequest
from doudou.observability.logging import get_logger
from doudou.tools.registry import tool_infos
from doudou.utils.text import truncate

logger = get_logger(__name__)

DEFAULT_CONVERSATION = "local"
MAX_TOOL_RESULT_CHARS = 300

_HELP = """\
[b]可用命令[/b]
  /new           开启新会话（换新的 thread，清空上下文）
  /history       查看当前会话的历史条数
  /tools         列出已装载的工具
  /help          显示本帮助
  /exit          退出

直接输入内容即向助手提问。"""


@dataclass(slots=True)
class _RenderState:
    """渲染过程中的临时状态。"""

    text_started: bool = False

    def reset(self) -> None:
        self.text_started = False


class CliChannel:
    """终端通道：读入用户输入，把 ReAct 事件渲染成人看的输出。

    它实现了 ``Channel`` 协议（便于 M4 之后与其他通道并列启停），
    同时直接被 ``doudou chat`` / ``doudou ask`` 使用。
    """

    name = "cli"

    def __init__(
        self,
        runner: AgentRunner,
        *,
        stream: bool = True,
        interactive: bool = True,
        echo_text: bool = True,
        console: Console | None = None,
        show_tool_calls: bool = True,
        max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
    ) -> None:
        self.runner = runner
        self.stream = stream
        self.interactive = interactive
        #: 是否把模型输出的增量正文渲染到 ``console``。
        #: ``False`` 时只聚合不渲染，由调用方决定怎么输出最终答复（``--quiet``）。
        self.echo_text = echo_text
        self.console = console or Console()
        self.show_tool_calls = show_tool_calls
        self.max_tool_result_chars = max_tool_result_chars
        self._state = _RenderState()

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

    async def ask(self, text: str, *, conversation_id: str = DEFAULT_CONVERSATION) -> RunResult:
        """跑一轮对话并渲染结果。"""
        request = RunRequest(text=text, conversation_id=conversation_id, channel=self.name)
        thread_id = await self.runner.resolve_thread(request)
        self._state.reset()

        if self.stream:
            result = await self._consume(
                self.runner.astream(request, thread_id=thread_id), thread_id=thread_id
            )
        else:
            result = await self.runner.ainvoke(request, thread_id=thread_id)

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

        return RunResult(
            text=text,
            steps=steps,
            messages=await self.runner.history(thread_id),
            usage=usage,
            events=events,
            ok=error is None,
            error=error,
        )

    def _render(self, event: RunEvent) -> None:
        if event.type == "text_delta":
            if not self.echo_text:
                return
            self._state.text_started = True
            self.console.print(event.text, end="", markup=False, highlight=False)
            return

        if event.type == "tool_call" and self.show_tool_calls:
            self._break_line()
            args = _format_args(event.tool_args or {})
            self.console.print(f"[dim]· 调用工具 [b]{event.tool_name}[/b]{args}[/dim]")
            return

        if event.type == "tool_result" and self.show_tool_calls:
            marker = "←" if event.tool_ok else "✗"
            body = truncate(event.result or "", self.max_tool_result_chars)
            style = "dim" if event.tool_ok else "bold red"
            self.console.print(f"[{style}]  {marker} {body}[/]")
            return

        if event.type == "error":
            self._break_line()
            self.console.print(f"[bold red]✗ {event.text}[/]")

    def _finish(self, result: RunResult) -> None:
        if not self.echo_text:
            return
        if self._state.text_started:
            self.console.print()
        elif result.text:
            # 非流式（或模型一次性返回）时补上正文。
            self.console.print(result.text, markup=False, highlight=False)

    def _break_line(self) -> None:
        """如果已经输出过正文，先换行，避免工具日志和正文粘在一起。"""
        if self._state.text_started:
            self.console.print()
            self._state.reset()

    # ---------------------------------------------------------------- 人工确认

    def approval_hook(self) -> ApprovalHook | None:
        """需要人工确认的工具回调；非交互模式下返回 ``None``。

        返回 ``None`` 时，``interrupt_on`` 中登记的工具会被拒绝执行，
        并把原因作为工具结果回填给模型（见 ``ReactAgent._execute_tool``）。
        """
        if not self.interactive:
            return None
        return make_approval_hook(self.console)

    # ---------------------------------------------------------------- REPL 命令

    def _print_banner(self) -> None:
        metadata = self.runner.bundle.metadata
        self.console.print(
            f"[bold]{self.runner.settings.app.agent.name}[/bold] "
            f"[dim]· 模型 {metadata.get('model')} · {len(self.runner.bundle.tools)} 个工具[/dim]"
        )
        self.console.print("[dim]输入 /help 查看命令，/exit 退出。[/dim]\n")

    async def _handle_command(self, command: str, *, conversation_id: str) -> bool:
        """处理斜杠命令；返回 ``False`` 表示应退出 REPL。

        是异步的：``/new`` 与 ``/history`` 要走 ``HistoryStore``（可能是数据库）。
        """
        name = command.split(maxsplit=1)[0].lower()

        if name in {"/exit", "/quit"}:
            return False
        if name == "/help":
            self.console.print(_HELP)
            return True
        if name == "/tools":
            self._print_tools()
            return True
        if name == "/new":
            thread_id = await self.runner.start_new_thread(conversation_id, channel=self.name)
            self.console.print(f"[green]已开启新会话[/green] [dim]{thread_id}[/dim]")
            return True
        if name == "/history":
            request = RunRequest(text="", conversation_id=conversation_id, channel=self.name)
            thread_id = await self.runner.resolve_thread(request)
            info = await self.runner.get_thread(thread_id)
            if info is None or info.message_count == 0:
                self.console.print("[dim]尚未开始对话。[/dim]")
            else:
                updated = f"{info.updated_at:%Y-%m-%d %H:%M:%S %Z}" if info.updated_at else "—"
                self.console.print(
                    f"[dim]thread={info.thread_id}，历史 {info.message_count} 条消息"
                    f"（更新于 {updated}）[/dim]"
                )
            return True

        self.console.print(f"[yellow]未知命令：{name}（/help 查看可用命令）[/yellow]")
        return True

    def _print_tools(self) -> None:
        table = Table(title="已装载的工具")
        table.add_column("名称", style="bold")
        table.add_column("分组", style="dim")
        table.add_column("副作用", justify="center")
        table.add_column("需确认", justify="center")
        table.add_column("说明")
        for info in tool_infos(self.runner.settings):
            table.add_row(
                info.name,
                info.group,
                "是" if info.side_effects else "—",
                "是" if info.requires_approval else "—",
                _summary(info.description),
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


def _summary(text: str, limit: int = 44) -> str:
    """把工具 docstring 压成表格可用的单行摘要（只取首行，避免撑爆行高）。"""
    stripped = text.strip()
    if not stripped:
        return ""
    return truncate(stripped.splitlines()[0].strip(), limit)


def _format_args(args: dict[str, Any]) -> str:
    if not args:
        return ""
    rendered = ", ".join(f"{key}={value!r}" for key, value in args.items())
    return f"({truncate(rendered, 120)})"


def _usage_text(usage: dict[str, int]) -> str:
    parts = [f"{key}={value}" for key, value in usage.items() if key.endswith("_tokens")]
    return "、".join(parts) if parts else "无用量统计"


__all__ = ["DEFAULT_CONVERSATION", "CliChannel", "make_approval_hook", "make_auto_approve_hook"]
