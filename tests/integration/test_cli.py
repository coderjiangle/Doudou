"""CLI 端到端：通过 typer 命令行入口跑完整链路（离线）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doudou.cli.main import app

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
    result = invoke("version")

    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "0.1.0" in result.stdout  # type: ignore[attr-defined]


def test_help_lists_commands() -> None:
    output = invoke("--help").stdout  # type: ignore[attr-defined]

    for command in ("chat", "ask", "tools", "skills", "config"):
        assert command in output


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
    assert "days_between" in result.stdout  # type: ignore[attr-defined]


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
    assert "mcp_config" in result.stdout  # type: ignore[attr-defined]


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
