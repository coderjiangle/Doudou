"""Linux 沙箱：用 bubblewrap（``bwrap``）隔离 shell 命令。

这里只做一件事：把一条命令包装成 ``bwrap`` 的 argv。策略参考 nanobot 的实现，
但只保留对「跑命令」真正必要的部分：

* 沙箱里的根是空的，只读挂载 ``/usr`` 等运行命令所需的系统路径；
* **只有工作区是读写绑定**，工作区的父目录用 tmpfs 遮住 —— ``config/``、``.env``、
  数据库文件都不会出现在沙箱里；
* ``/proc``、``/dev``、``/tmp`` 都是新实例，pid namespace 独立（沙箱里 ``ps`` 看不到宿主进程），
  ``HOME`` 指向 ``/tmp``（见 ``shell.py``）；
* **网络不隔离**：装依赖、``curl``、``git clone`` 仍然可用。这是刻意的取舍 ——
  隔离文件系统防的是「读到密钥 / 改坏宿主」，不是「不许联网」。

只在 Linux 上可用，且需要系统安装 bubblewrap（Debian/Ubuntu: ``apt install bubblewrap``）。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

#: 已实现的 backend。目前只有 bwrap；留成集合是为了以后加 nsjail / firejail 时不改配置形状。
SANDBOX_BACKENDS = ("bwrap",)

#: 必需的系统路径：没有 ``/usr`` 连 sh 都起不来。
_REQUIRED_RO = ("/usr",)

#: 尽力而为的只读路径（不存在就跳过），覆盖命令、动态库、DNS、证书、用户信息。
_OPTIONAL_RO = (
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc/alternatives",
    "/etc/ssl/certs",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/ld.so.cache",
    "/etc/passwd",
    "/etc/group",
    "/etc/localtime",
    "/etc/timezone",
)

#: 沙箱里的 HOME。宿主 home 不挂载，给一个可写的临时目录，避免 npm/pip 直接失败。
#: 这是沙箱内部的 tmpfs 路径，不是宿主的临时文件，所以显式忽略 S108。
SANDBOX_HOME = "/tmp"  # noqa: S108


def available(backend: str) -> tuple[bool, str]:
    """backend 是否可用；不可用时给出可读原因。"""
    if backend not in SANDBOX_BACKENDS:
        return False, f"未知的沙箱 backend：{backend!r}（可用：{', '.join(SANDBOX_BACKENDS)}）"
    if sys.platform != "linux":
        return False, f"沙箱 backend {backend} 只支持 Linux（当前平台 {sys.platform}）"
    if shutil.which(backend) is None:
        return False, f"未安装 {backend}：Debian/Ubuntu 执行 apt install bubblewrap"
    return True, ""


def build_argv(backend: str, command: str, *, workspace: str | Path, cwd: str | Path) -> list[str]:
    """把 ``command`` 包成 ``bwrap ... sh -c command`` 的 argv。

    Args:
        backend: 沙箱实现，目前只接受 ``bwrap``。
        command: 要执行的 shell 命令。
        workspace: 唯一可写的目录。
        cwd: 期望的工作目录；不在工作区内时退回工作区根目录。

    Raises:
        ValueError: backend 未知，或工作区是文件系统根目录（那等于没有隔离）。
    """
    if backend not in SANDBOX_BACKENDS:
        raise ValueError(f"未知的沙箱 backend：{backend!r}")

    ws = Path(workspace).resolve()
    if ws == Path(ws.anchor):  # "/" 或 Windows 盘符根
        raise ValueError("沙箱模式要求工作区不能是文件系统根目录")

    sandbox_cwd = _inside(ws, Path(cwd).resolve()) or ws

    argv = ["bwrap", "--new-session", "--die-with-parent", "--unshare-pid"]
    for path in _REQUIRED_RO:
        argv += ["--ro-bind", path, path]
    for path in _OPTIONAL_RO:
        argv += ["--ro-bind-try", path, path]

    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]  # noqa: S108 - 沙箱内 tmpfs

    # 遮住工作区的父目录：配置、.env、数据库都在那里，不该出现在沙箱里。
    parent = ws.parent
    if parent != ws and str(parent) != "/":
        argv += ["--tmpfs", str(parent)]

    argv += [
        "--dir",
        str(ws),
        "--bind",
        str(ws),
        str(ws),
        "--chdir",
        str(sandbox_cwd),
        "--",
        _shell_path(),
        "-c",
        command,
    ]
    return argv


def _inside(root: Path, candidate: Path) -> Path | None:
    """``candidate`` 在 ``root`` 内时返回它，否则返回 None。"""
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _shell_path() -> str:
    """宿主上 ``sh`` 的真实路径。

    沙箱里只挂载了 ``/usr`` 与少数 ``/bin``、``/lib``，所以不能假设 ``/bin/sh`` 一定存在
    （merged-usr 的发行版里它是个指向 ``/usr/bin/dash`` 的符号链接）。先用 which 找到，
    再解析成真实路径。
    """
    return str(Path(shutil.which("sh") or "/bin/sh").resolve())


__all__ = [
    "SANDBOX_BACKENDS",
    "SANDBOX_HOME",
    "available",
    "build_argv",
]
