"""Typer app: serve, chat, ask, tools, skills, config, version."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from doudou.agent.factory import AgentBundle, build_agent
from doudou.agent.models import ModelBuildError, credential_hint, has_credential, parse_spec
from doudou.agent.react import ApprovalHook
from doudou.channels.cli_channel import CliChannel, make_approval_hook, make_auto_approve_hook
from doudou.config.loader import Settings, load_settings
from doudou.gateway.runner import AgentRunner
from doudou.observability.logging import configure_logging
from doudou.skills.loader import discover_skills
from doudou.tools.registry import describe_groups, tool_infos
from doudou.utils.text import truncate
from doudou.version import __version__

app = typer.Typer(
    name="doudou",
    help="Doudou —— 一个名叫豆豆的个人AI助手",
    no_args_is_help=True,
    add_completion=False,
)
tools_app = typer.Typer(help="查看已装载的工具。", no_args_is_help=True)
skills_app = typer.Typer(help="查看已发现的 Skill。", no_args_is_help=True)
config_app = typer.Typer(help="查看与校验配置。", no_args_is_help=True)
app.add_typer(tools_app, name="tools")
app.add_typer(skills_app, name="skills")
app.add_typer(config_app, name="config")

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


def _load(config_dir: Path | None, *, log_level: str | None = None) -> Settings:
    """加载配置并按需初始化日志。"""
    # 先做一次最小初始化：否则配置加载阶段（如「配置文件不存在」）的日志
    # 会绕过统一出口打到默认 stderr。
    configure_logging(level=log_level or "INFO")

    try:
        settings = load_settings(config_dir=config_dir)
    except Exception as exc:
        err_console.print(f"[bold red]配置加载失败：[/bold red]{exc}")
        raise typer.Exit(EXIT_USAGE) from exc

    configure_logging(
        level=log_level or settings.app.runtime.log_level,
        json_logs=settings.app.runtime.log_json,
    )
    return settings


def _build_bundle(
    settings: Settings,
    model_spec: str | None,
    *,
    approval_hook: ApprovalHook | None = None,
) -> AgentBundle:
    try:
        return build_agent(settings, model_spec=model_spec, approval_hook=approval_hook)
    except ModelBuildError as exc:
        err_console.print(f"[bold red]模型不可用：[/bold red]{exc}")
        raise typer.Exit(EXIT_USAGE) from exc


def _make_channel(
    settings: Settings,
    model_spec: str | None,
    *,
    stream: bool,
    interactive: bool,
    quiet: bool,
) -> CliChannel:
    """组装 agent + runner + CLI 通道。

    顺序很重要：``ReactAgent`` 在创建时就固定了确认回调，
    因此必须先构造回调，再构造 agent。

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

    bundle = _build_bundle(settings, model_spec, approval_hook=hook)
    runner = AgentRunner(bundle)
    return CliChannel(
        runner,
        stream=stream,
        interactive=interactive,
        echo_text=not quiet,
        console=target,
        show_tool_calls=not quiet,
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
    config_dir: ConfigDir = None,
    log_level: LogLevel = None,
) -> None:
    """交互式对话（多轮，历史保存在内存中）。"""
    settings = _load(config_dir, log_level=log_level)
    channel = _make_channel(
        settings,
        model,
        stream=not no_stream,
        interactive=not no_confirm,
        quiet=False,
    )

    try:
        asyncio.run(channel.repl(conversation_id=conversation))
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        console.print("\n[dim]已中断。[/dim]")


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
    config_dir: ConfigDir = None,
    log_level: LogLevel = None,
) -> None:
    """单轮对话：提问 → 执行 ReAct 循环 → 输出答复 → 退出。"""
    settings = _load(config_dir, log_level=log_level)
    channel = _make_channel(
        settings,
        model,
        stream=not no_stream,
        interactive=not no_confirm and not quiet,
        quiet=quiet,
    )

    result = asyncio.run(channel.ask(question, conversation_id=conversation))

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
) -> None:
    """列出所有已装载的工具。"""
    settings = _load(config_dir)
    infos = tool_infos(settings)
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
                f"  [yellow]{status.name}[/yellow] [dim]（{label}：{status.reason}）[/dim]"
            )

    if show_args:
        for info in infos:
            console.print(f"[bold]{info.name}[/bold] {info.args}")


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
    """列出已发现的 Skill。"""
    settings = _load(config_dir)
    roots = settings.skills_dirs
    skills, errors = discover_skills(roots)

    if not roots:
        console.print("[yellow]配置里未声明 Skill 目录（agent.skills）。[/yellow]")
        return

    table = Table(title=f"Skill（{len(skills)}）")
    table.add_column("名称", style="bold")
    table.add_column("目录", style="dim")
    table.add_column("说明")
    for info in skills:
        table.add_row(
            info.name,
            str(info.directory),
            _summary(info.description),
        )
    console.print(table)
    for root in roots:
        if not root.is_dir():
            console.print(f"[yellow]目录不存在：{root}[/yellow]")
    for message in errors:
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
    console.print(f"config_dir    : {settings.config_dir}")
    console.print(f"config_file   : {settings.config_file or '（未找到，使用默认值）'}")
    console.print(f"loaded_files  : {[str(item) for item in settings.loaded_files]}")
    console.print(f"root_dir      : {settings.root_dir}")
    console.print(f"system_prompt : {settings.system_prompt_path}")
    console.print(f"skills_dirs   : {[str(item) for item in settings.skills_dirs]}")
    console.print(f"mcp_config    : {settings.mcp_config_path}")


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
                ("FEISHU_APP_ID", settings.secrets.feishu_app_id),
                ("FEISHU_APP_SECRET", settings.secrets.feishu_app_secret),
            )
            if not value
        ]
        if missing:
            problems.append(("warn", f"飞书通道已启用但缺少：{', '.join(missing)}"))
        if not settings.secrets.feishu_encrypt_key:
            problems.append(("warn", "飞书未配置 FEISHU_ENCRYPT_KEY，生产环境应启用加密"))

    skills, errors = discover_skills(settings.skills_dirs)
    for message in errors:
        problems.append(("error", f"Skill 校验失败：{message}"))

    infos = tool_infos(settings)
    if not infos:
        problems.append(("warn", "没有任何可用工具，Agent 只能凭模型知识作答"))

    skipped = [status for status in describe_groups(settings) if not status.enabled]
    for status in skipped:
        level = "info" if status.reason.startswith("未列入") else "warn"
        problems.append((level, f"工具分组 {status.name} 未启用：{status.reason}"))

    tool_names = ", ".join(info.name for info in infos) or "（无）"
    skill_names = ", ".join(info.name for info in skills) or "（无）"
    guarded = [info.name for info in infos if info.requires_approval]
    console.print(f"[green]✓ 配置合法[/green]（{len(settings.loaded_files)} 个文件）")
    console.print(f"  模型      : {spec.raw}")
    console.print(f"  工具      : {len(infos)} 个 —— {tool_names}")
    console.print(f"  需人工确认: {', '.join(guarded) if guarded else '（无）'}")
    console.print(f"  Skill     : {len(skills)} 个 —— {skill_names}")
    console.print(f"  飞书通道  : {'启用' if feishu.enabled else '关闭'}")

    for level, message in problems:
        style = {"error": "red", "warn": "yellow", "info": "dim"}[level]
        marker = {"error": "!", "warn": "!", "info": "·"}[level]
        console.print(f"[{style}]  {marker} {message}[/]")

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
        log_config=None,
        access_log=False,
    )


@app.command()
def version() -> None:
    """打印版本号。"""
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
    """``python -m doudou`` 的入口。"""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
