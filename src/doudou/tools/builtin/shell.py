"""Shell execution tool, optionally sandboxed with bubblewrap on Linux.

**默认不注册**，必须显式打开 ``tools.shell.enabled: true``。

为什么这么保守：这是本仓库唯一**没有路径约束机制**的出口 ——

* 路径权限规则对它无效（``agent.permissions`` 检查的是我们需要解析的路径，
  而 ``rm -rf /`` 根本不经过我们的解析）；
* 工作目录限制只是「起点的默认位置」，命令里写绝对路径就绕过了；
* 能拦住的只有人工确认这一道。

因此把它做成显式开关：默认关着，打开就意味着接受这些风险。
在 Linux 上可以再打开 ``tools.shell.sandbox: bwrap``，用 bubblewrap 把命令关进
「只有工作区可写」的文件系统视图（见 :mod:`doudou.tools.builtin.sandbox`）。
沙箱不隔离网络，也不替代容器 —— 它只是让「写坏宿主 / 读到密钥」变得困难。

其它限制：输出上限、执行超时、环境变量白名单（不把 API Key 透传给子进程）。
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from doudou.config.loader import Settings
from doudou.observability.logging import get_logger
from doudou.tools.builtin import sandbox
from doudou.tools.decorators import annotate
from doudou.utils.text import truncate

logger = get_logger(__name__)

GROUP = "shell"

#: 输出被截断时保留的尾部长度 —— 报错信息通常在最后。
TAIL_CHARS = 2000


class ShellError(ValueError):
    """命令执行失败。消息会直接回填给模型。"""


def missing_requirement(settings: Settings) -> str | None:
    """shell 分组不可用的原因；可用时返回 ``None``。

    ``build_tools`` 与 ``describe_groups`` 共用这一份判断，避免「工具没注册」
    与「列表里说已启用」对不上。
    """
    config = settings.app.tools.shell
    if not config.enabled:
        return "tools.shell.enabled 为 false（默认关闭）"
    if not config.sandbox:
        return None

    usable, reason = sandbox.available(config.sandbox)
    if not usable:
        return reason

    workspace = settings.root_dir
    if workspace == Path(workspace.anchor):
        return "沙箱模式要求工作区不能是文件系统根目录"
    if config.cwd:
        cwd = _resolve_cwd(settings, config.cwd, create=False)
        if not cwd.is_relative_to(workspace):
            return f"tools.shell.cwd 必须位于工作区（{workspace}）内"
    return None


def build_tools(settings: Settings) -> Sequence[BaseTool]:
    """构造命令执行工具；未启用或沙箱不可用时返回空列表。"""
    config = settings.app.tools.shell
    reason = missing_requirement(settings)
    if reason:
        logger.debug("tools.shell_skipped", reason=reason)
        return []

    workspace = settings.root_dir
    cwd = _resolve_cwd(settings, config.cwd)
    sandboxed = bool(config.sandbox)
    env = _build_env(config.inherit_env, sandboxed=sandboxed)

    @tool
    async def shell_exec(command: str) -> str:
        """在工作目录里执行一条 shell 命令。

        Args:
            command: 要执行的命令，例如 ``ls -la``、``git status``。

        Returns:
            退出码与输出（stdout / stderr 合并，超长会截断）。

        只在确实需要操作系统能力时使用（跑测试、查文件、调 CLI）。
        命令会先请用户确认；**不要**用它做能用其它工具完成的事
        （读写工作区文件用 workspace_*，取时间用 now）。
        """
        if not command.strip():
            raise ShellError("命令不能为空。")

        logger.info(
            "tools.shell_started",
            cwd=str(cwd),
            sandbox=config.sandbox or None,
            command=truncate(command, 200),
        )
        started = asyncio.get_running_loop().time()
        try:
            if sandboxed:
                argv = sandbox.build_argv(config.sandbox, command, workspace=workspace, cwd=cwd)
                process = await _spawn_sandboxed(argv, cwd=workspace, env=env)
            else:
                process = await _spawn_shell(command, cwd=cwd, env=env)
        except OSError as exc:
            raise ShellError(f"无法启动命令：{exc}") from exc
        except ValueError as exc:
            raise ShellError(str(exc)) from exc
        except NotImplementedError as exc:  # pragma: no cover - 平台相关
            # Windows 的 Selector 事件循环不支持子进程；uvicorn 默认用 Proactor，
            # 只有显式改过事件循环策略时才会走到这里。
            raise ShellError(
                "当前事件循环不支持子进程（Windows 上需要 Proactor 事件循环）。"
            ) from exc

        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=config.timeout)
        except TimeoutError as exc:
            await _terminate(process, group=sandboxed)
            raise ShellError(
                f"命令执行超时（>{config.timeout}s），已终止。如果是长任务，请拆分或改用其它方式。"
            ) from exc

        elapsed = round(asyncio.get_running_loop().time() - started, 3)
        output = (stdout or b"").decode("utf-8", errors="replace")
        body = _shape_output(output, config.max_output_chars)
        logger.info(
            "tools.shell_finished",
            exit_code=process.returncode,
            elapsed=elapsed,
            output_chars=len(output),
        )
        return f"退出码 {process.returncode}（耗时 {elapsed}s）\n{body}"

    annotate(shell_exec, group=GROUP, side_effects=True, timeout=config.timeout + 10)
    return [shell_exec]


async def _spawn_shell(
    command: str,
    *,
    cwd: Path,
    env: dict[str, str],
) -> asyncio.subprocess.Process:
    """直接交给系统 shell（POSIX 是 ``/bin/sh``，Windows 是 ``cmd.exe``）。"""
    return await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def _spawn_sandboxed(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> asyncio.subprocess.Process:
    """用 argv 启动 bwrap（不经过外层 shell，避免二次解析与引号问题）。"""
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # 新进程组：超时时按组收尸，bwrap 里的子进程不会留在后台。
        start_new_session=True,
    )


async def _terminate(process: asyncio.subprocess.Process, *, group: bool) -> None:
    """超时后终止进程；沙箱模式下连整个进程组一起杀。

    ``killpg`` / ``getpgid`` / ``SIGKILL`` 在 Windows 的 typeshed 里不存在，
    因此用 ``getattr`` 取；沙箱本身只在 Linux 上用得到。
    """
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    sigkill = getattr(signal, "SIGKILL", None)

    try:
        if group and killpg is not None and getpgid is not None and sigkill is not None:
            killpg(getpgid(process.pid), sigkill)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        await process.wait()
    except (ProcessLookupError, OSError):
        pass


def _resolve_cwd(settings: Settings, configured: str | None, *, create: bool = True) -> Path:
    """工作目录：默认工作区根目录，可被 ``tools.shell.cwd`` 覆盖。"""
    if not configured:
        cwd = settings.root_dir
    else:
        path = Path(configured).expanduser()
        cwd = path if path.is_absolute() else Path.cwd() / path
    if create:
        cwd.mkdir(parents=True, exist_ok=True)
    return cwd.resolve()


def _build_env(inherit: Sequence[str], *, sandboxed: bool = False) -> dict[str, str]:
    """只透传白名单里的环境变量。

    默认不继承整个环境：那会把 ``OPENAI_API_KEY``、``FEISHU_APP_SECRET``
    之类一并交给子进程，而子进程可能是模型临时拼出来的命令。

    沙箱里没有宿主的 ``HOME``，这里指向 ``/tmp``：npm / pip 之类需要一个可写的
    home 与缓存目录，否则会在「跟隔离无关」的地方失败。
    """
    env = {key: os.environ[key] for key in inherit if key in os.environ}
    # Windows 上子进程仍需要 SystemRoot 才能正常跑命令。
    for extra in ("SystemRoot", "COMSPEC", "PATHEXT"):
        if extra in os.environ:
            env.setdefault(extra, os.environ[extra])
    if sandboxed:
        env["HOME"] = sandbox.SANDBOX_HOME
        env["TMPDIR"] = sandbox.SANDBOX_HOME
        env.setdefault("XDG_CACHE_HOME", f"{sandbox.SANDBOX_HOME}/.cache")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _shape_output(output: str, limit: int) -> str:
    """截断输出，但保留头尾 —— 报错信息通常在最后。"""
    text = output.strip() or "（没有输出）"
    if len(text) <= limit:
        return text
    head = text[: limit - TAIL_CHARS]
    tail = text[-TAIL_CHARS:]
    return f"{head}\n…（已截断 {len(text) - limit} 字）…\n{tail}"


def default_shell_hint() -> str:
    """给 ``config check`` 用的提示（默认不开时告知怎么开）。"""
    return (
        "shell_exec 默认禁用；需要时在 config.yaml 里设 tools.shell.enabled: true"
        f"（当前平台 {sys.platform}）。Linux 上可再加 tools.shell.sandbox: bwrap 隔离文件系统。"
    )


__all__ = ["GROUP", "ShellError", "build_tools", "missing_requirement"]
