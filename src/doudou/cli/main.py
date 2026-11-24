"""Typer app: serve, chat, ask, tools, skills, config, version."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from langchain_core.tools import BaseTool
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from doudou.agent.factory import AgentBundle, build_agent
from doudou.agent.models import ModelBuildError, credential_hint, has_credential, parse_spec
from doudou.agent.react import ApprovalHook, RunResult
from doudou.channels.cli_channel import CliChannel, make_approval_hook, make_auto_approve_hook
from doudou.config.loader import Settings, load_settings
from doudou.gateway.runner import AgentRunner
from doudou.mcp.client import ServerOverview
from doudou.mcp.config import McpConfigError
from doudou.mcp.lifecycle import McpRuntime
from doudou.observability.logging import configure_logging, get_logger
from doudou.scheduler.store import SchedulerStore
from doudou.skills.registry import SkillRegistry
from doudou.storage.database import Database, create_database
from doudou.storage.scheduled import SqlSchedulerStore
from doudou.tools.registry import build_tools, describe_groups, tool_infos
from doudou.utils.console import configure_stdio
from doudou.utils.optional import install_hint, missing_modules, module_available
from doudou.utils.text import truncate
from doudou.version import __version__

logger = get_logger(__name__)

app = typer.Typer(
    name="doudou",
    help="Doudou —— 一个名叫豆豆的个人AI助手",
    no_args_is_help=True,
    add_completion=False,
)
tools_app = typer.Typer(help="查看已装载的工具。", no_args_is_help=True)
skills_app = typer.Typer(help="查看已发现的 Skill。", no_args_is_help=True)
config_app = typer.Typer(help="查看与校验配置。", no_args_is_help=True)
mcp_app = typer.Typer(help="查看与探测 MCP server。", no_args_is_help=True)
voice_app = typer.Typer(help="本地常驻语音助手（sherpa-onnx + DeepSeek ASR/TTS）。")
app.add_typer(tools_app, name="tools")
app.add_typer(skills_app, name="skills")
app.add_typer(config_app, name="config")
app.add_typer(mcp_app, name="mcp")
app.add_typer(voice_app, name="voice")

console = Console()
err_console = Console(stderr=True)

ConfigDir = Annotated[
    Path | None,
    typer.Option("--config-dir", help="配置目录，默认 ./config。"),
]
ModelOption = Annotated[
    str | None,
    typer.Option("--model", "-m", help="覆盖模型，如 openai:gpt-5.5 或 scripted:demo。"),
]
LogLevel = Annotated[
    str | None,
    typer.Option("--log-level", help="日志级别：DEBUG/INFO/WARNING/ERROR。"),
]

EXIT_OK = 0
EXIT_USAGE = 2


# --------------------------------------------------------------------------- 内部工具


def _load(
    config_dir: Path | None,
    *,
    log_level: str | None = None,
    default_log_level: str | None = None,
) -> Settings:
    """加载配置并按需初始化日志。"""
    # 先做一次最小初始化：否则配置加载阶段（如「配置文件不存在」）的日志
    # 会绕过统一出口打到默认 stderr。
    configure_logging(level=log_level or default_log_level or "INFO")

    try:
        settings = load_settings(config_dir=config_dir)
    except Exception as exc:
        err_console.print(f"[bold red]配置加载失败：[/bold red]{exc}")
        raise typer.Exit(EXIT_USAGE) from exc

    # 交互对话命令（chat / ask / voice）默认压低到 WARNING，避免 INFO 日志穿插进对话输出；
    # 显式传入 --log-level 时仍以用户指定为准。
    effective_level = log_level or default_log_level or settings.app.runtime.log_level
    configure_logging(
        level=effective_level,
        json_logs=settings.app.runtime.log_json,
        timezone=settings.app.runtime.timezone,
    )
    return settings


def _build_bundle(
    settings: Settings,
    model_spec: str | None,
    *,
    approval_hook: ApprovalHook | None = None,
    extra_tools: Sequence[BaseTool] = (),
    scheduler_store: SchedulerStore | None = None,
) -> AgentBundle:
    try:
        return build_agent(
            settings,
            model_spec=model_spec,
            approval_hook=approval_hook,
            extra_tools=extra_tools,
            scheduler_store=scheduler_store,
        )
    except ModelBuildError as exc:
        err_console.print(f"[bold red]模型不可用：[/bold red]{exc}")
        raise typer.Exit(EXIT_USAGE) from exc


@dataclass
class CliSession:
    """CLI 一次会话持有的资源：通道 +（可选）MCP 运行期与调度存储的数据库。"""

    channel: CliChannel
    mcp: McpRuntime | None = None
    database: Database | None = None

    async def aclose(self) -> None:
        if self.mcp is not None:
            await self.mcp.stop()
        if self.database is not None:
            await self.database.dispose()


async def _open_scheduler_store(
    settings: Settings,
) -> tuple[SchedulerStore | None, Database | None]:
    """为 CLI 打开调度存储。

    CLI 本身是无状态的，但定时任务必须落库才能被 ``serve`` 实例触发，所以这里
    按 ``storage.database_url`` 单独开一个库。开不起来就**安静降级**（不注册工具），
    而不是让 ``doudou ask`` 直接失败。
    """
    if not settings.app.scheduler.enabled:
        return None, None
    try:
        database = create_database(settings.app.storage)
        await database.create_schema()
    except Exception as exc:
        logger.warning("scheduler.cli_store_unavailable", error=str(exc))
        return None, None
    return SqlSchedulerStore(database), database


async def _prepare_channel(
    settings: Settings,
    model_spec: str | None,
    *,
    stream: bool,
    interactive: bool,
    quiet: bool,
    render_markdown: bool = True,
) -> CliSession:
    """组装 agent + runner + CLI 通道，并拉取 MCP 工具。

    顺序很重要：``ReactAgent`` 在创建时就固定了工具列表与确认回调，
    因此必须先拉 MCP 工具、先构造回调，再构造 agent。

    确认策略三选一：

    * 默认 —— 询问用户（``interactive=True``）；
    * ``--no-confirm`` —— 一律同意（用户明确表示不想被打断）；
    * ``--quiet`` —— 不提供回调。无回调时副作用工具会被**拒绝**，
      这适合脚本场景：没有人在场就不能替用户同意写操作。
    """
    target = err_console if quiet else console
    if quiet:
        hook: ApprovalHook | None = None
    elif interactive:
        hook = make_approval_hook(target)
    else:
        hook = make_auto_approve_hook(target)

    mcp = McpRuntime(settings)
    mcp_tools = await mcp.start()
    scheduler_store, database = await _open_scheduler_store(settings)

    bundle = _build_bundle(
        settings,
        model_spec,
        approval_hook=hook,
        extra_tools=mcp_tools,
        scheduler_store=scheduler_store,
    )
    runner = AgentRunner(bundle)
    channel = CliChannel(
        runner,
        stream=stream,
        interactive=interactive,
        echo_text=not quiet,
        console=target,
        show_tool_calls=not quiet,
        render_markdown=settings.app.channels.cli.render_markdown and render_markdown,
    )
    return CliSession(channel=channel, mcp=mcp if mcp.enabled else None, database=database)


def _make_channel(
    settings: Settings,
    model_spec: str | None,
    *,
    stream: bool,
    interactive: bool,
    quiet: bool,
    render_markdown: bool = True,
) -> CliChannel:
    """同步构造通道（不含 MCP）。

    保留它是因为 ``doudou chat`` 之外还有少量同步调用点；新代码请用
    :func:`_prepare_channel`，否则 MCP 工具不会生效。
    """
    target = err_console if quiet else console
    if quiet:
        hook: ApprovalHook | None = None
    elif interactive:
        hook = make_approval_hook(target)
    else:
        hook = make_auto_approve_hook(target)

    bundle = _build_bundle(settings, model_spec, approval_hook=hook)
    runner = AgentRunner(bundle)
    return CliChannel(
        runner,
        stream=stream,
        interactive=interactive,
        echo_text=not quiet,
        console=target,
        show_tool_calls=not quiet,
        render_markdown=settings.app.channels.cli.render_markdown and render_markdown,
    )


def _cli_channel(
    runner: AgentRunner,
    *,
    stream: bool,
    interactive: bool,
    quiet: bool,
) -> CliChannel:
    return CliChannel(
        runner,
        stream=stream,
        interactive=interactive,
        console=err_console if quiet else console,
        show_tool_calls=not quiet,
    )


def _summary(text: str, limit: int = 44) -> str:
    """把工具的 docstring 压成表格可用的单行摘要。

    只取**首行**：工具的 docstring 首行本来就是一句话说明，
    后面的 ``Args:`` / ``Returns:`` 是给模型看的，塞进表格只会把行高撑爆。
    """
    stripped = text.strip()
    if not stripped:
        return ""
    return truncate(stripped.splitlines()[0].strip(), limit)


# --------------------------------------------------------------------------- 对话


@app.command()
def chat(
    model: ModelOption = None,
    conversation: Annotated[str, typer.Option("--conversation", "-c", help="会话标识。")] = "local",
    no_stream: Annotated[bool, typer.Option("--no-stream", help="关闭流式输出。")] = False,
    no_confirm: Annotated[
        bool,
        typer.Option("--no-confirm", help="不询问，直接同意有副作用的工具执行。"),
    ] = False,
    raw: Annotated[bool, typer.Option("--raw", help="关闭 Markdown 渲染，按纯文本输出。")] = False,
    config_dir: ConfigDir = None,
    log_level: LogLevel = None,
) -> None:
    """交互式对话（多轮，历史保存在内存中）。"""
    settings = _load(config_dir, log_level=log_level, default_log_level="WARNING")

    async def _run() -> None:
        prepared = await _prepare_channel(
            settings,
            model,
            stream=not no_stream,
            interactive=not no_confirm,
            quiet=False,
            render_markdown=not raw,
        )
        try:
            await prepared.channel.repl(conversation_id=conversation)
        finally:
            await prepared.aclose()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        console.print("\n[dim]已中断。[/dim]")


# --------------------------------------------------------------------------- 语音


@voice_app.callback(invoke_without_command=True)
def voice_listen(
    ctx: typer.Context,
    model: ModelOption = None,
    conversation: Annotated[
        str, typer.Option("--conversation", "-c", help="语音会话标识。")
    ] = "voice-local",
    no_confirm: Annotated[
        bool,
        typer.Option("--no-confirm", help="不询问，直接同意有副作用的工具执行。"),
    ] = False,
    config_dir: ConfigDir = None,
    log_level: LogLevel = None,
) -> None:
    """启动常驻本地语音交互（以「豆豆」开头唤醒，音箱流式播报）。"""
    if ctx.invoked_subcommand is not None:
        return

    from doudou.channels.voice.deps import ensure_voice_dependencies
    from doudou.channels.voice.errors import VoiceError

    try:
        ensure_voice_dependencies()
    except VoiceError as exc:
        err_console.print(f"[bold red]语音启动失败：[/bold red]{escape(str(exc))}")
        raise typer.Exit(EXIT_USAGE) from exc

    from doudou.channels.voice.assistant import VoiceAssistant

    settings = _load(config_dir, log_level=log_level, default_log_level="WARNING")

    async def _run() -> None:
        prepared = await _prepare_channel(
            settings,
            model,
            stream=True,
            interactive=not no_confirm,
            quiet=False,
        )
        try:
            assistant = VoiceAssistant(
                settings,
                prepared.channel.runner,
                console=console,
                conversation_id=conversation,
            )
            await assistant.run()
        finally:
            await prepared.aclose()

    try:
        asyncio.run(_run())
    except VoiceError as exc:
        err_console.print(f"[bold red]语音启动失败：[/bold red]{exc}")
        raise typer.Exit(EXIT_USAGE) from exc
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        console.print("\n[dim]语音监听已停止。[/dim]")


@voice_app.command("download-models")
def voice_download_models(config_dir: ConfigDir = None) -> None:
    """预下载本地 sherpa-onnx 唤醒与 VAD 模型并生成唤醒词表。"""
    from doudou.channels.voice.deps import VOICE_MODEL_MODULES, ensure_voice_dependencies
    from doudou.channels.voice.errors import VoiceError

    try:
        ensure_voice_dependencies(VOICE_MODEL_MODULES)
    except VoiceError as exc:
        err_console.print(f"[bold red]下载失败：[/bold red]{escape(str(exc))}")
        raise typer.Exit(EXIT_USAGE) from exc

    from doudou.channels.voice.models import ensure_voice_models
    from doudou.config.loader import resolve_path

    settings = _load(config_dir)
    voice_cfg = settings.app.channels.voice
    models_dir = resolve_path(Path.cwd(), voice_cfg.models_dir)
    try:
        paths = asyncio.run(
            ensure_voice_models(
                models_dir,
                wake_words=voice_cfg.wake_words,
                auto_download=True,
            )
        )
    except VoiceError as exc:
        err_console.print(f"[bold red]下载失败：[/bold red]{exc}")
        raise typer.Exit(EXIT_USAGE) from exc
    console.print(f"[green]✓ 语音模型已就绪[/green]：{paths.vad_model.parent}")
    console.print(f"  唤醒词表：{paths.kws_keywords}")


@voice_app.command("devices")
def voice_devices() -> None:
    """列出当前系统可用的麦克风与音箱设备。"""
    from doudou.channels.voice.deps import VOICE_DEVICE_MODULES, ensure_voice_dependencies
    from doudou.channels.voice.errors import VoiceError

    try:
        ensure_voice_dependencies(VOICE_DEVICE_MODULES)
    except VoiceError as exc:
        err_console.print(f"[bold red]无法列出音频设备：[/bold red]{escape(str(exc))}")
        raise typer.Exit(EXIT_USAGE) from exc

    try:
        import sounddevice as sd

        devices = sd.query_devices()
        default_in, default_out = sd.default.device
    except Exception as exc:
        err_console.print(f"[bold red]无法读取音频设备：[/bold red]{escape(str(exc))}")
        raise typer.Exit(EXIT_USAGE) from exc
    table = Table(title="音频设备")
    table.add_column("编号", justify="right", style="bold")
    table.add_column("名称")
    table.add_column("输入声道", justify="right")
    table.add_column("输出声道", justify="right")
    table.add_column("默认")
    for idx, dev in enumerate(devices):
        marks: list[str] = []
        if idx == default_in:
            marks.append("输入")
        if idx == default_out:
            marks.append("输出")
        table.add_row(
            str(idx),
            str(dev["name"]),
            str(dev["max_input_channels"]),
            str(dev["max_output_channels"]),
            "/".join(marks) or "—",
        )
    console.print(table)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="要提问的内容。")],
    model: ModelOption = None,
    conversation: Annotated[str, typer.Option("--conversation", "-c", help="会话标识。")] = "local",
    no_stream: Annotated[bool, typer.Option("--no-stream", help="关闭流式输出。")] = False,
    no_confirm: Annotated[
        bool,
        typer.Option("--no-confirm", help="不询问，直接同意有副作用的工具执行。"),
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="只输出最终答复，适合脚本调用。")
    ] = False,
    raw: Annotated[bool, typer.Option("--raw", help="关闭 Markdown 渲染，按纯文本输出。")] = False,
    config_dir: ConfigDir = None,
    log_level: LogLevel = None,
) -> None:
    """单轮对话：提问 → 执行 ReAct 循环 → 输出答复 → 退出。"""
    settings = _load(config_dir, log_level=log_level, default_log_level="WARNING")

    async def _run() -> RunResult:
        prepared = await _prepare_channel(
            settings,
            model,
            stream=not no_stream,
            interactive=not no_confirm and not quiet,
            quiet=quiet,
            render_markdown=not raw,
        )
        try:
            return await prepared.channel.ask(question, conversation_id=conversation)
        finally:
            await prepared.aclose()

    result = asyncio.run(_run())

    if quiet:
        if result.text:
            console.print(result.text, markup=False, highlight=False)
        if not result.ok:
            err_console.print(f"[red]{result.error}[/red]")
    if not result.ok:
        raise typer.Exit(EXIT_USAGE)


# --------------------------------------------------------------------------- 自省


@tools_app.command("list")
def tools_list(
    config_dir: ConfigDir = None,
    show_args: Annotated[bool, typer.Option("--args", help="显示参数列表。")] = False,
    no_mcp: Annotated[bool, typer.Option("--no-mcp", help="不拉取 MCP 工具（只看内置）。")] = False,
) -> None:
    """列出 Agent 实际装载的工具（默认包含 MCP 拉取到的那些）。"""
    settings = _load(config_dir)

    async def _collect() -> tuple[list[Any], McpRuntime | None, Database | None]:
        scheduler_store, database = await _open_scheduler_store(settings)
        if no_mcp:
            return build_tools(settings, scheduler_store=scheduler_store), None, database
        runtime = McpRuntime(settings)
        mcp_tools = await runtime.start()
        return (
            build_tools(
                settings,
                extra=mcp_tools,
                extra_source="mcp",
                scheduler_store=scheduler_store,
            ),
            runtime,
            database,
        )

    tools, runtime, database = asyncio.run(_collect())
    try:
        infos = tool_infos(settings, tools=tools)
    finally:
        if runtime is not None:
            asyncio.run(runtime.stop())
        if database is not None:
            asyncio.run(database.dispose())

    table = Table(title=f"工具（{len(infos)}）")
    table.add_column("名称", style="bold")
    table.add_column("分组", style="dim")
    table.add_column("来源", style="dim")
    table.add_column("副作用", justify="center")
    table.add_column("需确认", justify="center")
    table.add_column("说明")
    for info in infos:
        table.add_row(
            info.name,
            info.group,
            info.source,
            "是" if info.side_effects else "—",
            "是" if info.requires_approval else "—",
            _summary(info.description),
        )
    console.print(table)

    skipped = [status for status in describe_groups(settings) if not status.enabled]
    if skipped:
        console.print("\n[dim]未启用的分组：[/dim]")
        for status in skipped:
            label = "被配置关闭" if status.reason.startswith("未列入") else "依赖不满足"
            console.print(
                f"  [yellow]{status.name}[/yellow] [dim]（{label}：{escape(status.reason)}）[/dim]"
            )

    if show_args:
        for info in infos:
            console.print(f"[bold]{info.name}[/bold] {info.args}")

    # MCP 是运行时拉取的，不在 provider 里；单独报一下每个 server 的结果，
    # 否则「工具怎么少了」只能靠翻日志。
    if runtime is not None:
        for entry in runtime.describe():
            if entry.ok:
                console.print(
                    f"[dim]· MCP {entry.name}：{len(entry.tools)} 个工具"
                    f"（{entry.transport}，{entry.elapsed}s）[/dim]"
                )
            else:
                console.print(f"[red]· MCP {entry.name} 不可用：{entry.error}[/red]")
            if entry.filtered_out:
                console.print(f"[dim]  已过滤：{', '.join(entry.filtered_out)}[/dim]")


@tools_app.command("show")
def tools_show(
    name: Annotated[str, typer.Argument(help="工具名。")],
    config_dir: ConfigDir = None,
) -> None:
    """显示单个工具的完整信息。"""
    settings = _load(config_dir)
    infos = {info.name: info for info in tool_infos(settings)}
    info = infos.get(name)
    if info is None:
        err_console.print(f"[red]没有名为 {name!r} 的工具。[/red]可用：{', '.join(sorted(infos))}")
        raise typer.Exit(EXIT_USAGE)
    console.print_json(json.dumps(info.as_dict(), ensure_ascii=False))


@skills_app.command("list")
def skills_list(config_dir: ConfigDir = None) -> None:
    """列出已发现的 Skill（索引 + 附属文件）。"""
    settings = _load(config_dir)
    registry = SkillRegistry.from_settings(settings)
    roots = registry.roots

    if not roots:
        console.print("[yellow]配置里未声明 Skill 目录（agent.skills）。[/yellow]")
        return

    table = Table(title=f"Skill（{len(registry)}）")
    table.add_column("名称", style="bold")
    table.add_column("正文", justify="right", style="dim")
    table.add_column("附件", justify="right", style="dim")
    table.add_column("说明")
    for entry in registry.describe():
        table.add_row(
            entry.name,
            str(entry.body_chars),
            str(len(entry.files)),
            _summary(entry.description),
        )
    console.print(table)

    for root in roots:
        if not root.is_dir():
            console.print(f"[yellow]目录不存在：{root}[/yellow]")
    for message in registry.errors:
        console.print(f"[red]· {message}[/red]")


@skills_app.command("show")
def skills_show(
    name: Annotated[str, typer.Argument(help="技能名。")],
    config_dir: ConfigDir = None,
    body: Annotated[bool, typer.Option("--body", help="同时打印正文。")] = False,
) -> None:
    """查看一个 Skill 的详情。"""
    settings = _load(config_dir)
    registry = SkillRegistry.from_settings(settings)
    skill = registry.get(name)
    if skill is None:
        err_console.print(
            f"[red]没有名为 {name!r} 的技能。[/red]可用：{'、'.join(registry.names) or '（无）'}"
        )
        raise typer.Exit(EXIT_USAGE)

    console.print(f"[bold]{skill.name}[/bold]")
    console.print(f"  说明 : {skill.description}")
    console.print(f"  文件 : {skill.path}")
    console.print(f"  正文 : {len(skill.body)} 字")
    files = registry.list_files(name)
    console.print(f"  附件 : {'、'.join(files) if files else '（无）'}")
    if skill.extra:
        console.print(f"  其它 : {skill.extra}")
    if body:
        console.print()
        console.print(skill.body)


@skills_app.command("check")
def skills_check(config_dir: ConfigDir = None) -> None:
    """只校验 Skill 目录（等价于 ``config check`` 的 Skill 部分）。"""
    settings = _load(config_dir)
    registry = SkillRegistry.from_settings(settings)

    console.print(f"根目录：{[str(root) for root in registry.roots]}")
    console.print(f"有效   ：{len(registry)} 个 —— {'、'.join(registry.names) or '（无）'}")
    if not registry.errors:
        console.print("[green]✓ 全部通过校验[/green]")
        return

    for message in registry.errors:
        console.print(f"[red]· {message}[/red]")
    raise typer.Exit(EXIT_USAGE)


@skills_app.command("reload")
def skills_reload(config_dir: ConfigDir = None) -> None:
    """重新扫描 Skill 目录（等价于 ``POST /skills/reload``）。"""
    settings = _load(config_dir)
    registry = SkillRegistry.from_settings(settings)

    console.print(f"[green]已重新扫描[/green]：{len(registry)} 个")
    console.print(f"  {'、'.join(registry.names) or '（无）'}")
    for message in registry.errors:
        console.print(f"[red]· {message}[/red]")


@config_app.command("show")
def config_show(
    config_dir: ConfigDir = None,
    as_yaml: Annotated[bool, typer.Option("--yaml", help="以 YAML 输出。")] = False,
) -> None:
    """打印合并后的生效配置（密钥自动脱敏）。"""
    settings = _load(config_dir)
    payload = settings.describe()
    if as_yaml:
        import yaml

        console.print(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
    else:
        console.print_json(json.dumps(payload, ensure_ascii=False))


@config_app.command("path")
def config_path(config_dir: ConfigDir = None) -> None:
    """打印配置文件与关键目录的实际路径。"""
    settings = _load(config_dir)
    console.print(f"home_dir      : {settings.home_dir}")
    console.print(f"config_dir    : {settings.config_dir}")
    console.print(f"config_file   : {settings.config_file or '（未找到，使用默认值）'}")
    console.print(f"loaded_files  : {[str(item) for item in settings.loaded_files]}")
    console.print(f"root_dir      : {settings.root_dir}")
    console.print(f"system_prompt : {settings.system_prompt_path}")
    console.print(f"skills_dirs   : {[str(item) for item in settings.skills_dirs]}")
    console.print(
        f"mcp_servers   : {len(settings.app.mcp.servers)} 个（config.yaml 的 mcp.servers）"
    )


@config_app.command("check")
def config_check(config_dir: ConfigDir = None, model: ModelOption = None) -> None:
    """校验配置、模型凭据、工具与 Skill，打印可操作的问题清单。"""
    settings = _load(config_dir)
    problems: list[tuple[str, str]] = []  # (级别, 消息)

    spec = parse_spec(model or settings.app.model.primary)
    hint = credential_hint(spec)
    if hint and not has_credential(settings.secrets, spec):
        problems.append(("warn", f"{hint[1]}（或改用 --model scripted:demo 离线体验）"))

    feishu = settings.app.channels.feishu
    if feishu.enabled:
        missing = [
            name
            for name, value in (
                ("DOUDOU_CHANNELS_FEISHU_APP_ID", feishu.app_id),
                ("DOUDOU_CHANNELS_FEISHU_APP_SECRET", feishu.app_secret),
            )
            if not value
        ]
        if missing:
            problems.append(("warn", f"飞书通道已启用但缺少：{', '.join(missing)}"))
        if not feishu.encrypt_key:
            problems.append(
                (
                    "warn",
                    "飞书未配置 DOUDOU_CHANNELS_FEISHU_ENCRYPT_KEY，生产环境应启用加密",
                )
            )
        if not module_available("lark_oapi"):
            problems.append(
                ("error", f"飞书通道已启用但未安装 lark-oapi。{install_hint('feishu')}")
            )

    voice = settings.app.channels.voice
    if voice.enabled:
        from doudou.channels.voice.deps import VOICE_MODULES

        missing_voice = missing_modules(VOICE_MODULES)
        if missing_voice:
            problems.append(
                (
                    "error",
                    f"语音通道已启用但缺少可选依赖：{', '.join(missing_voice)}。"
                    f"{install_hint('voice')}",
                )
            )

    skill_registry = SkillRegistry.from_settings(settings)
    for message in skill_registry.errors:
        problems.append(("error", f"Skill 校验失败：{message}"))

    infos = tool_infos(settings)
    if not infos:
        problems.append(("warn", "没有任何可用工具，Agent 只能凭模型知识作答"))

    skipped = [status for status in describe_groups(settings) if not status.enabled]
    for status in skipped:
        # 「被配置关掉」与「未列入 enabled_groups」都是用户的明确选择，不该每次都告警。
        quiet = status.reason.startswith(("未列入", "tools.shell.enabled"))
        level = "info" if quiet else "warn"
        problems.append((level, f"工具分组 {status.name} 未启用：{status.reason}"))

    tool_names = ", ".join(info.name for info in infos) or "（无）"
    skill_names = "、".join(skill_registry.names) or "（无）"
    guarded = [info.name for info in infos if info.requires_approval]
    console.print(f"[green]✓ 配置合法[/green]（{len(settings.loaded_files)} 个文件）")
    console.print(f"  模型      : {spec.raw}")
    console.print(f"  工具      : {len(infos)} 个 —— {tool_names}")
    console.print(f"  需人工确认: {', '.join(guarded) if guarded else '（无）'}")
    console.print(f"  Skill     : {len(skill_registry)} 个 —— {skill_names}")
    console.print(f"  飞书通道  : {'启用' if feishu.enabled else '关闭'}")

    for level, message in problems:
        style = {"error": "red", "warn": "yellow", "info": "dim"}[level]
        marker = {"error": "!", "warn": "!", "info": "·"}[level]
        console.print(f"[{style}]  {marker} {escape(message)}[/]")

    if any(level == "error" for level, _ in problems):
        raise typer.Exit(EXIT_USAGE)


# --------------------------------------------------------------------------- 其他


@app.command()
def serve(
    config_dir: ConfigDir = None,
    reload: Annotated[bool, typer.Option("--reload", help="热重载（开发用）。")] = False,
    host: Annotated[str | None, typer.Option("--host", help="覆盖监听地址。")] = None,
    port: Annotated[int | None, typer.Option("--port", help="覆盖监听端口。")] = None,
    log_level: LogLevel = None,
) -> None:
    """启动 FastAPI 服务。"""
    settings = _load(config_dir, log_level=log_level)
    server = settings.app.server
    resolved_host = host or server.host
    resolved_port = port or server.port

    import uvicorn

    console.print(
        f"[green]启动 [bold]{settings.app.agent.name}[/bold][/green] "
        f"[dim]· 模型 {settings.app.model.primary} · 历史 {settings.app.storage.history} "
        f"· 鉴权 {'开启' if settings.api_token else '关闭'}[/dim]"
    )
    console.print(
        f"[dim]接口: http://{resolved_host}:{resolved_port}{server.base_path}"
        f" · 文档: http://{resolved_host}:{resolved_port}/docs[/dim]"
    )

    # log_config=None：不要覆盖我们已经配好的 structlog 出口。
    # access_log=False：访问日志由 AccessLogMiddleware 以结构化形式输出。
    uvicorn.run(
        "doudou.api.app:app",
        host=resolved_host,
        port=resolved_port,
        reload=reload,
        workers=server.workers,
        log_config=None,
        access_log=False,
    )


@app.command()
def init(
    home: Annotated[
        Path | None,
        typer.Option("--home", help="初始化根目录，默认 ~/.doudou。"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="覆盖已存在的配置、提示词与 Skill 模板（不覆盖 .env 与 AGENTS.md）。",
        ),
    ] = False,
) -> None:
    """初始化 ~/.doudou 目录、配置、工作区与 Skill。"""
    from doudou.config.env import read_env
    from doudou.config.paths import doudou_home
    from doudou.init import InitError, init_home

    if home is None:
        configured_home = read_env().get("DOUDOU_HOME")
        home = Path(configured_home) if configured_home else doudou_home()
    try:
        result = init_home(home=home, force=force)
    except (InitError, OSError) as exc:
        err_console.print(f"[bold red]初始化失败：[/bold red]{escape(str(exc))}")
        raise typer.Exit(EXIT_USAGE) from exc

    console.print(f"[green]✓ 已初始化[/green] {result.home}")
    console.print(f"  配置   : {result.config_dir}")
    console.print(f"  数据   : {result.data_dir}")
    console.print(f"  工作区 : {result.workspace_dir}")
    console.print(f"  Skill  : {result.skills_dir}")
    if result.skipped:
        console.print(
            f"[dim]已跳过 {len(result.skipped)} 个已存在文件；需要覆盖请加 --force。[/dim]"
        )
    if home is not None:
        console.print(f"[dim]后续命令请设置 DOUDOU_HOME={result.home}[/dim]")
    console.print(
        f"[dim]下一步：编辑 {result.home / '.env'} 填入模型密钥，然后运行 "
        "doudou config check。[/dim]"
    )


@app.command()
def version() -> None:
    """打印版本号。"""
    console.print(__version__)


# --------------------------------------------------------------------------- MCP


@mcp_app.command("list")
def mcp_list(
    config_dir: ConfigDir = None,
    as_json: Annotated[bool, typer.Option("--json", help="以 JSON 输出。")] = False,
) -> None:
    """列出 MCP server、拉取到的工具与被过滤掉的工具。"""
    settings = _load(config_dir)

    async def _run() -> list[ServerOverview]:
        runtime = McpRuntime(settings)
        try:
            await runtime.start()
            return runtime.describe()
        finally:
            await runtime.stop()

    try:
        entries = asyncio.run(_run())
    except McpConfigError as exc:
        err_console.print(f"[bold red]MCP 配置有误：[/bold red]{exc}")
        raise typer.Exit(EXIT_USAGE) from exc

    if as_json:
        console.print_json(json.dumps([entry.as_dict() for entry in entries], ensure_ascii=False))
        return
    if not entries:
        console.print("[yellow]没有配置任何 MCP server。[/yellow]")
        console.print(
            "[dim]在 config/config.yaml 的 mcp.servers 里加一项即可（那里有模板）。[/dim]"
        )
        return

    table = Table(title=f"MCP server（{len(entries)}）")
    table.add_column("名称", style="bold")
    table.add_column("传输", style="dim")
    table.add_column("状态", justify="center")
    table.add_column("工具数", justify="right")
    table.add_column("只读", justify="center")
    table.add_column("地址 / 命令", style="dim")
    for entry in entries:
        table.add_row(
            entry.name,
            entry.transport,
            "[green]ok[/green]" if entry.ok else "[red]失败[/red]",
            str(len(entry.tools)),
            "是" if entry.read_only else "否",
            truncate(entry.target, 46),
        )
    console.print(table)
    for entry in entries:
        if entry.error and not entry.ok:
            console.print(f"[red]· {entry.name}：{entry.error}[/red]")
        if entry.filtered_out:
            console.print(f"[dim]· {entry.name} 过滤掉：{', '.join(entry.filtered_out)}[/dim]")


@mcp_app.command("check")
def mcp_check(
    server: Annotated[str, typer.Argument(help="要探测的 server 名称。")],
    config_dir: ConfigDir = None,
) -> None:
    """单独探测一个 MCP server 能否拉到工具。"""
    settings = _load(config_dir)

    async def _run() -> tuple[list[ServerOverview], dict[str, Any]]:
        runtime = McpRuntime(settings)
        try:
            await runtime.refresh(reason="cli-check")
            info = await runtime.manager.server_info()
            return runtime.describe(), info
        finally:
            await runtime.stop()

    try:
        entries, info = asyncio.run(_run())
    except McpConfigError as exc:
        err_console.print(f"[bold red]探测失败：[/bold red]{exc}")
        raise typer.Exit(EXIT_USAGE) from exc

    entry = next((item for item in entries if item.name == server), None)
    if entry is None:
        names = ", ".join(item.name for item in entries) or "（无）"
        err_console.print(f"[red]没有名为 {server!r} 的 server。[/red]可用：{names}")
        raise typer.Exit(EXIT_USAGE)

    if entry.ok:
        console.print(f"[green]✓ {server} 可用[/green]（耗时 {entry.elapsed}s）")
    else:
        console.print(f"[red]✗ {server} 不可用[/red]：{entry.error}")
    console.print(f"  传输   : {entry.transport}")
    console.print(f"  工具   : {', '.join(entry.tools) or '（无）'}")
    if entry.filtered_out:
        console.print(f"  已过滤 : {', '.join(entry.filtered_out)}")
    if server in info:
        console.print(f"  服务端 : {info[server]}")
    if not entry.ok:
        raise typer.Exit(EXIT_USAGE)
    console.print(__version__)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    show_version: Annotated[
        bool, typer.Option("--version", "-V", help="显示版本号并退出。")
    ] = False,
) -> None:
    """Doudou 命令行入口。"""
    if show_version:
        console.print(__version__)
        raise typer.Exit(EXIT_OK)
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(EXIT_OK)


def main() -> None:
    """``python -m doudou`` 与 ``doudou`` 命令的入口。"""
    configure_stdio()
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
