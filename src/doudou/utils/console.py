"""控制台编码：避免 Windows 的 GBK 代码页把输出弄崩。

Windows 控制台默认代码页是 936（GBK），写 ``✓``、``…`` 或任何 GBK 之外的字符都会抛
``UnicodeEncodeError`` —— ``doudou config check`` 曾经因此在最后一步直接崩掉，而输出
本来已经算好了。

处理分两种情况：

* **交互式终端**：把代码页切到 65001 并把标准流重设为 UTF-8，中文与符号都能正常显示；
* **重定向 / 管道**：不改编码（保持系统默认，PowerShell 等下游才能正确解码），
  只把错误处理改成 ``replace`` —— 宁可显示 ``?``，也不要让整条命令失败。

其它平台什么也不做。
"""

from __future__ import annotations

import sys
from typing import Any


def configure_stdio() -> None:
    """让标准输出/错误输出在 Windows 上不会因为编码问题崩掉。

    幂等，可以在 CLI 入口和 ASGI 入口各调一次。失败时静默跳过 —— 编码问题
    不该反过来把进程弄挂。
    """
    if sys.platform != "win32":
        return

    _set_windows_code_page()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if _is_terminal(stream):
                reconfigure(encoding="utf-8", errors="replace")
            else:
                reconfigure(errors="replace")
        except (OSError, ValueError):
            # 流可能被包装成不支持重配置的对象（测试、嵌入场景）。
            continue


def _is_terminal(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


def _set_windows_code_page() -> None:
    """把控制台输入/输出代码页设为 65001（UTF-8）。"""
    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:  # pragma: no cover - 只在非 Windows 上出现
            return
        kernel32 = windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:  # pragma: no cover - 没有控制台（服务、重定向）时
        return


__all__ = ["configure_stdio"]
