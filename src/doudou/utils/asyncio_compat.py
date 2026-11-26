"""Windows asyncio 运行期兼容处理。

Python 3.12 的 Proactor 事件循环在「对端已经重置连接、本端随后关闭传输」时，
``_ProactorBasePipeTransport._call_connection_lost`` 里的 ``socket.shutdown()``
会抛出 ``ConnectionResetError``。这个回调已经是连接丢失后的清理路径，异常
没有可处理的业务含义；但 asyncio 的默认异常处理器会把它打印成一整段
traceback，在 CLI 里看起来像是聊天过程中崩了一次。

这里只过滤这一类已知噪音：异常类型必须是 ``ConnectionResetError``，且抛出点
必须是 ``_call_connection_lost`` 清理回调。请求本身如果真的失败，异常仍会从
``httpx`` / 任务里抛出来，和这里无关。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: CPython Proactor 传输对象上的「连接已丢失」清理回调。
_CONNECTION_LOST_CALLBACK = "_call_connection_lost"

#: 标记异常处理器已经装过，避免重复包装。
_INSTALLED_ATTR = "_doudou_connection_reset_filter"


def _is_connection_reset_in_transport_close(context: dict[str, Any]) -> bool:
    """判断异常是否来自 Proactor 关闭传输时的 ``shutdown()``。

    只看两个特征：异常是连接重置，且回调是 ``_call_connection_lost``。
    其它场合的连接重置（例如读请求体时）必须原样暴露。
    """
    if not isinstance(context.get("exception"), ConnectionResetError):
        return False
    handle = context.get("handle")
    callback = getattr(handle, "_callback", None)
    return getattr(callback, "__name__", "") == _CONNECTION_LOST_CALLBACK


def install_connection_reset_filter(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """在当前（或指定）事件循环上过滤 Windows Proactor 的连接重置噪音。

    非 Windows 平台直接返回。安装是幂等的；已经安装过时不会重复包一层。
    其它异常仍交给原来的处理器（没有原处理器时走 ``default_exception_handler``）。
    """
    if sys.platform != "win32":
        return

    target = loop or asyncio.get_running_loop()
    if getattr(target, _INSTALLED_ATTR, False):
        return

    previous = target.get_exception_handler()

    def _filtered(loop_: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if _is_connection_reset_in_transport_close(context):
            logger.debug(
                "asyncio.connection_reset_ignored",
                transport=repr(context.get("transport")),
                error=str(context.get("exception")),
            )
            return
        if previous is not None:
            previous(loop_, context)
        else:
            loop_.default_exception_handler(context)

    target.set_exception_handler(_filtered)
    # AbstractEventLoop 没有这个属性；用 setattr 打标记只是为了幂等，
    # 不参与任何业务逻辑。
    setattr(target, _INSTALLED_ATTR, True)


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run`` 的薄包装：运行前先装好连接重置过滤器。"""

    async def _main() -> T:
        install_connection_reset_filter()
        return await coro

    return asyncio.run(_main())


__all__ = ["install_connection_reset_filter", "run_async"]
