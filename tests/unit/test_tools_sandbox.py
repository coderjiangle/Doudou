"""bubblewrap 沙箱：argv 组装、可用性判断，以及 shell 分组的门控。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from doudou.tools.builtin import sandbox, shell
from doudou.tools.registry import describe_groups


def _values(argv: list[str], flag: str) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == flag]


# --------------------------------------------------------------------------- argv


def test_wraps_command_into_bwrap(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()

    argv = sandbox.build_argv("bwrap", "echo hi", workspace=ws, cwd=ws)

    assert argv[0] == "bwrap"
    assert "--new-session" in argv
    assert "--die-with-parent" in argv
    # 没有 pid namespace 的话，沙箱里 `ps` 能看到宿主进程（命令行里可能有密钥）。
    assert "--unshare-pid" in argv
    assert str(ws.resolve()) in _values(argv, "--bind")
    assert _values(argv, "--chdir") == [str(ws.resolve())]

    # 命令本身原样放在 ``--`` 之后，由沙箱里的 sh -c 执行。
    separator = argv.index("--")
    assert argv[separator + 2 :] == ["-c", "echo hi"]


def test_workspace_parent_is_masked(tmp_path: Path) -> None:
    """父目录里有 config/、.env、数据库，必须用 tmpfs 遮住。"""
    ws = tmp_path / "data" / "workspace"
    ws.mkdir(parents=True)

    argv = sandbox.build_argv("bwrap", "ls", workspace=ws, cwd=ws)

    assert str(ws.resolve().parent) in _values(argv, "--tmpfs")


def test_cwd_inside_workspace_is_preserved(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    sub = ws / "src" / "lib"
    sub.mkdir(parents=True)

    argv = sandbox.build_argv("bwrap", "pwd", workspace=ws, cwd=sub)

    assert _values(argv, "--chdir") == [str(sub.resolve())]


def test_cwd_outside_workspace_falls_back_to_root(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()

    argv = sandbox.build_argv("bwrap", "pwd", workspace=ws, cwd=tmp_path / "other")

    assert _values(argv, "--chdir") == [str(ws.resolve())]


def test_root_workspace_is_rejected() -> None:
    with pytest.raises(ValueError, match="根目录"):
        sandbox.build_argv("bwrap", "ls", workspace=Path("/"), cwd=Path("/"))


def test_unknown_backend_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知"):
        sandbox.build_argv("nsjail", "ls", workspace=tmp_path, cwd=tmp_path)


# --------------------------------------------------------------------------- 可用性


def test_unknown_backend_not_available() -> None:
    usable, reason = sandbox.available("nsjail")

    assert usable is False
    assert "未知" in reason


def test_not_available_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    usable, reason = sandbox.available("bwrap")

    assert usable is False
    assert "Linux" in reason


def test_not_available_without_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)

    usable, reason = sandbox.available("bwrap")

    assert usable is False
    assert "bubblewrap" in reason


def test_available_on_linux_with_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")

    assert sandbox.available("bwrap") == (True, "")


# --------------------------------------------------------------------------- 分组门控


def test_shell_disabled_by_default(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(config_yaml="")

    reason = shell.missing_requirement(settings)

    assert reason is not None
    assert "tools.shell.enabled" in reason
    assert shell.build_tools(settings) == []


def test_sandbox_unavailable_skips_tool(settings_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml="tools:\n  shell:\n    enabled: true\n    sandbox: bwrap\n"
    )
    monkeypatch.setattr(sandbox, "available", lambda backend: (False, "未安装 bwrap"))

    assert shell.missing_requirement(settings) == "未安装 bwrap"
    assert shell.build_tools(settings) == []

    status = {item.name: item for item in describe_groups(settings)}["shell"]
    assert status.enabled is False
    assert "bwrap" in status.reason


def test_sandbox_requires_cwd_inside_workspace(
    settings_factory,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    settings = settings_factory(
        config_yaml=(
            f'filesystem:\n  root_dir: "{workspace.as_posix()}"\n'
            "tools:\n  shell:\n    enabled: true\n    sandbox: bwrap\n"
            f'    cwd: "{outside.as_posix()}"\n'
        )
    )
    monkeypatch.setattr(sandbox, "available", lambda backend: (True, ""))

    reason = shell.missing_requirement(settings)

    assert reason is not None
    assert "必须位于工作区" in reason


def test_sandbox_rejects_root_workspace(settings_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml='filesystem:\n  root_dir: "/"\ntools:\n  shell:\n    enabled: true\n    sandbox: bwrap\n'
    )
    monkeypatch.setattr(sandbox, "available", lambda backend: (True, ""))

    reason = shell.missing_requirement(settings)

    assert reason is not None
    assert "根目录" in reason


# --------------------------------------------------------------------------- 环境变量


def test_sandbox_env_moves_home_into_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/someone")

    sandboxed = shell._build_env(["HOME", "PATH"], sandboxed=True)
    plain = shell._build_env(["HOME", "PATH"], sandboxed=False)

    assert sandboxed["HOME"] == sandbox.SANDBOX_HOME
    assert sandboxed["TMPDIR"] == sandbox.SANDBOX_HOME
    assert plain["HOME"] == "/home/someone"


async def test_shell_exec_closes_stdin_so_stdin_reads_do_not_hang(
    settings_factory,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """``shell_exec`` 必须把 stdin 设为 DEVNULL，避免读标准输入的命令卡死到超时。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = settings_factory(
        config_yaml=(
            f'filesystem:\n  root_dir: "{workspace.as_posix()}"\n'
            "tools:\n  shell:\n    enabled: true\n    timeout: 2\n"
        )
    )
    tool = shell.build_tools(settings)[0]
    output = await tool.ainvoke(
        {"command": f'"{sys.executable}" -c "import sys; print(len(sys.stdin.read()))"'}
    )
    assert "退出码 0" in output
    assert "\n0" in output
