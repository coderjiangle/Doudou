"""CLI 端到端：通过 typer 命令行入口跑完整链路（离线）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from doudou.channels.cli_channel import CliChannel
from doudou.cli.main import app
from doudou.tools.builtin import browser

runner = CliRunner()

CONFIG = """\
version: 1
runtime:
  log_level: ERROR
  timezone: Asia/Shanghai
agent:
  name: CliTest
  system_prompt_file: null
  skills: []
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    root = tmp_path / "config"
    root.mkdir()
    (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
    return root


def invoke(*args: str) -> object:
    return runner.invoke(app, list(args), catch_exceptions=False)


# --------------------------------------------------------------------------- 基础


def test_version() -> None:
    from doudou.version import __version__

    result = invoke("version")

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert __version__ in result.stdout  # type: ignore[attr-defined]


def test_help_lists_commands() -> None:
    output = invoke("--help").stdout  # type: ignore[attr-defined]

    for command in ("chat", "ask", "tools", "skills", "config", "init"):
        assert command in output


def test_chat_exit_stops_browser_driver(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """实际走 /exit，验证在创建驱动的事件循环中等待 stop() 完成。"""
    monkeypatch.chdir(config_dir.parent)
    monkeypatch.setenv("DOUDOU_HOME", str(config_dir.parent))
    monkeypatch.setenv("DOUDOU_TOOLS_BROWSER_ENABLED", "true")
    monkeypatch.setenv("DOUDOU_MCP_ENABLED", "false")
    monkeypatch.setenv("DOUDOU_SCHEDULER_ENABLED", "false")
    monkeypatch.setattr(browser, "playwright_available", lambda: True)
    started: list[asyncio.AbstractEventLoop] = []
    stopped: list[asyncio.AbstractEventLoop] = []

    async def stop() -> None:
        await asyncio.sleep(0)
        stopped.append(asyncio.get_running_loop())

    async def list_tabs(backend: browser.PlaywrightBrowserBackend) -> str:
        started.append(asyncio.get_running_loop())
        monkeypatch.setattr(backend, "_playwright", SimpleNamespace(stop=stop))
        return "test tab"

    original_repl = CliChannel.repl

    async def repl(channel: CliChannel, *, conversation_id: str) -> None:
        tool = next(tool for tool in channel.runner.bundle.tools if tool.name == "browser_tabs")
        assert await tool.ainvoke({}) == "test tab"
        await original_repl(channel, conversation_id=conversation_id)

    monkeypatch.setattr(browser.PlaywrightBrowserBackend, "list_tabs", list_tabs)
    monkeypatch.setattr(CliChannel, "repl", repl)
    result = runner.invoke(
        app,
        ["chat", "--local", "--model", "scripted:demo", "--config-dir", str(config_dir)],
        input="/exit\n",
    )

    assert result.exit_code == 0
    assert "再见。" in result.output
    assert len(started) == 1
    assert stopped == started


def test_chat_ephemeral_serve_starts_replies_and_stops_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认 doudou chat 会拉起临时 serve 并在 /exit 时关闭，不留常驻进程。"""
    home = tmp_path / "home"
    cfg = home / "config"
    cfg.mkdir(parents=True)
    db_path = (home / "data" / "doudou.db").as_posix()
    (cfg / "config.yaml").write_text(
        f"""\
version: 1
runtime:
  log_level: ERROR
  timezone: Asia/Shanghai
server:
  host: 127.0.0.1
  port: 18931
agent:
  name: EphemeralTest
  system_prompt_file: null
  skills: []
storage:
  database_url: sqlite+aiosqlite:///{db_path}
  history: database
mcp:
  enabled: false
scheduler:
  enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOUDOU_HOME", str(home))
    monkeypatch.setenv("DOUDOU_MODEL_PRIMARY", "scripted:demo")

    from doudou.cli.remote import EphemeralServeProcess

    seen_owned: list[bool] = []
    seen_procs: list[object] = []
    orig_attach = EphemeralServeProcess.attach_or_spawn

    async def tracked_attach(cls, *args, **kwargs):  # type: ignore[no-untyped-def]
        proc = await orig_attach(*args, **kwargs)
        seen_owned.append(proc.owned)
        seen_procs.append(proc._process)
        return proc

    monkeypatch.setattr(EphemeralServeProcess, "attach_or_spawn", classmethod(tracked_attach))

    result = runner.invoke(
        app,
        ["chat", "--model", "scripted:demo", "--config-dir", str(cfg)],
        input="你好\n/history\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "[offline] 收到你的消息：你好" in result.output
    assert "历史 2 条消息" in result.output
    assert "再见。" in result.output
    assert "runner.run_finished" not in result.output
    assert seen_owned == [True]
    assert len(seen_procs) == 1
    assert seen_procs[0].poll() is not None  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- 单轮对话


def test_ask_runs_tool_then_answers(config_dir: Path) -> None:
    """单轮对话：模型要求调用 now → 真实执行 → 把结果写进答复。"""
    result = invoke(
        "ask",
        "现在几点了？",
        "--model",
        "scripted:demo",
        "--config-dir",
        str(config_dir),
        "--quiet",
    )

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "工具返回结果" in result.stdout  # type: ignore[attr-defined]
    assert "CST" in result.stdout  # type: ignore[attr-defined]


def test_ask_without_tool(config_dir: Path) -> None:
    result = invoke(
        "ask",
        "你好",
        "--model",
        "scripted:demo",
        "--config-dir",
        str(config_dir),
        "--quiet",
    )

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "[offline] 收到你的消息：你好" in result.stdout  # type: ignore[attr-defined]


def test_ask_raw_option_prints_plain_text(config_dir: Path) -> None:
    result = invoke(
        "ask",
        "你好",
        "--model",
        "scripted:demo",
        "--config-dir",
        str(config_dir),
        "--raw",
    )

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "[offline] 收到你的消息：你好" in result.stdout  # type: ignore[attr-defined]


def test_ask_defaults_log_level_to_warning_unless_explicit(tmp_path: Path) -> None:
    """即使配置里 runtime.log_level=INFO，交互 CLI 默认也应压到 WARNING；显式 --log-level INFO 可恢复。"""
    root = tmp_path / "cfg"
    root.mkdir()
    (root / "config.yaml").write_text(
        CONFIG.replace("log_level: ERROR", "log_level: INFO"),
        encoding="utf-8",
    )

    quiet_logs = invoke("ask", "你好", "--model", "scripted:demo", "--config-dir", str(root))
    assert quiet_logs.exit_code == 0  # type: ignore[attr-defined]
    assert "runner.run_finished" not in quiet_logs.output  # type: ignore[attr-defined]

    verbose_logs = invoke(
        "ask",
        "你好",
        "--model",
        "scripted:demo",
        "--config-dir",
        str(root),
        "--log-level",
        "INFO",
    )
    assert verbose_logs.exit_code == 0  # type: ignore[attr-defined]
    assert "runner.run_finished" in verbose_logs.output  # type: ignore[attr-defined]


def test_ask_stream_and_non_stream_agree(config_dir: Path) -> None:
    """流式与非流式的**答复结构**应当一致。

    刻意不比较完整输出：这个用例问的是「现在几点了」，工具返回真实时间戳，
    两次运行跨过秒边界就会不同 —— 那是个 flaky 断言（确实偶发失败过）。
    """
    common = [
        "ask",
        "现在几点了？",
        "--model",
        "scripted:demo",
        "--config-dir",
        str(config_dir),
        "--quiet",
    ]

    streamed = invoke(*common).stdout  # type: ignore[attr-defined]
    plain = invoke(*common, "--no-stream").stdout  # type: ignore[attr-defined]

    for output in (streamed, plain):
        assert "工具返回结果" in output
        assert "CST" in output
    assert len(streamed.splitlines()) == len(plain.splitlines())


def test_ask_reports_unusable_model(config_dir: Path) -> None:
    """模型不可用时给出可操作的错误，而不是抛栈。"""
    result = invoke(
        "ask", "你好", "--model", "scripted:unknown-model", "--config-dir", str(config_dir)
    )

    assert result.exit_code == 2  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- 自省


def test_tools_list(config_dir: Path) -> None:
    result = invoke("tools", "list", "--config-dir", str(config_dir))

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "now" in result.stdout  # type: ignore[attr-defined]


def test_tools_show_json(config_dir: Path) -> None:
    result = invoke("tools", "show", "now", "--config-dir", str(config_dir))

    assert result.exit_code == 0  # type: ignore[attr-defined]
    payload = json.loads(result.stdout)  # type: ignore[attr-defined]
    assert payload["name"] == "now"
    assert payload["group"] == "clock"


def test_tools_show_unknown_name(config_dir: Path) -> None:
    result = invoke("tools", "show", "nope", "--config-dir", str(config_dir))

    assert result.exit_code == 2  # type: ignore[attr-defined]


def test_config_check_succeeds_with_warning(config_dir: Path) -> None:
    """没有模型凭据只告警，不阻塞 —— 用户可以用离线模型继续。"""
    result = invoke("config", "check", "--config-dir", str(config_dir))

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "配置合法" in result.stdout  # type: ignore[attr-defined]


def test_config_path_lists_derived_paths(config_dir: Path) -> None:
    result = invoke("config", "path", "--config-dir", str(config_dir))

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "root_dir" in result.stdout  # type: ignore[attr-defined]
    assert "mcp_servers" in result.stdout  # type: ignore[attr-defined]


def test_serve_starts_uvicorn(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve` 必须真的把 uvicorn 拉起来，且不能覆盖我们的日志配置。

    这里把 ``uvicorn.run`` 换成假实现：真跑起来会一直阻塞（它是个服务器）。
    """
    import uvicorn

    captured: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = invoke("serve", "--config-dir", str(config_dir), "--host", "0.0.0.0", "--port", "9123")

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert captured["app"] == "doudou.api.app:app"
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9123
    # 这两项决定日志出口：覆盖 log_config 会丢掉 structlog 配置，
    # 打开 access_log 会和我们的 AccessLogMiddleware 重复输出。
    assert captured["log_config"] is None
    assert captured["access_log"] is False


def test_serve_uses_configured_host_and_port(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 --host/--port 时用配置里的值。"""
    import uvicorn

    captured: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))

    result = invoke("serve", "--config-dir", str(config_dir))

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8000
