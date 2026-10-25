"""Token 计数。

上下文工程的每个决定（裁剪、压缩、卸载）都以 token 为单位，而不是消息条数 ——
一条 5 万字的工具结果和一条「好的」在条数上等价，在预算上差三个数量级。

计数是估算：用 ``cl100k_base`` 去估一个非 OpenAI 模型只是近似，但对上下文工程
够用，我们关心的是量级和趋势，所以要的只是一个便宜、稳定、可比较的读数。

``tiktoken.get_encoding`` 首次使用会下载词表，内网或离线部署下会失败，此时回退到
字符启发式，并且只告警一次 —— 每次调用都刷日志比不准更糟。
"""

from __future__ import annotations

import json
from typing import Any

from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 估算用的编码。``cl100k_base`` 对多数 OpenAI 兼容端点都足够接近。
ENCODING_NAME = "cl100k_base"

#: 单条消息的固定开销（角色、分隔符等）。经验值，与 OpenAI 的文档口径一致。
MESSAGE_OVERHEAD_TOKENS = 4

_encoding: Any | None = None
_encoding_ready = False
_fallback_warned = False


def get_encoding() -> Any | None:
    """懒加载 tiktoken 编码；不可用时返回 ``None`` 并告警一次。"""
    global _encoding, _encoding_ready, _fallback_warned
    if _encoding_ready:
        return _encoding
    _encoding_ready = True
    try:
        import tiktoken

        _encoding = tiktoken.get_encoding(ENCODING_NAME)
        logger.debug("tokens.encoder_ready", encoding=ENCODING_NAME)
    except Exception as exc:
        _encoding = None
        if not _fallback_warned:
            _fallback_warned = True
            logger.warning(
                "tokens.encoder_unavailable",
                encoding=ENCODING_NAME,
                error=str(exc),
                fallback="按字符估算（CJK 每字约 1 token，其它每 4 字符约 1 token）",
            )
    return _encoding


def reset_encoder_cache() -> None:
    """清空编码缓存（测试用）。"""
    global _encoding, _encoding_ready, _fallback_warned
    _encoding = None
    _encoding_ready = False
    _fallback_warned = False


def estimate_tokens(text: str) -> int:
    """字符启发式：CJK 每字约 1 token，其余每 4 字符约 1 token。"""
    if not text:
        return 0
    cjk = 0
    for char in text:
        code = ord(char)
        if (
            0x4E00 <= code <= 0x9FFF  # 汉字
            or 0x3000 <= code <= 0x303F  # CJK 标点
            or 0xFF00 <= code <= 0xFFEF  # 全角
            or 0x3040 <= code <= 0x30FF  # 日文假名
        ):
            cjk += 1
    other = len(text) - cjk
    return cjk + max(0, other // 4)


def count_tokens(text: str) -> int:
    """估算一段文本的 token 数。空文本为 0。"""
    if not text:
        return 0
    encoding = get_encoding()
    if encoding is None:
        return estimate_tokens(text)
    try:
        return len(encoding.encode(text))
    except Exception as exc:
        logger.debug("tokens.encode_failed", error=str(exc))
        return estimate_tokens(text)


def count_message_tokens(message: Any) -> int:
    """估算一条消息的 token 数（含角色开销与工具调用参数）。"""
    from doudou.utils.text import content_to_text

    total = MESSAGE_OVERHEAD_TOKENS
    total += count_tokens(content_to_text(getattr(message, "content", None)))

    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        try:
            total += count_tokens(json.dumps(tool_calls, ensure_ascii=False, default=str))
        except (TypeError, ValueError):  # pragma: no cover - 保底
            total += count_tokens(str(tool_calls))

    # 工具名/工具调用 id 也会进请求，忽略它们会让估算系统性偏低。
    for attr in ("name", "tool_call_id"):
        value = getattr(message, attr, None)
        if value:
            total += count_tokens(str(value))
    return total


def count_messages_tokens(messages: Any) -> int:
    """估算一组消息的总 token 数。"""
    return sum(count_message_tokens(message) for message in messages)


__all__ = [
    "ENCODING_NAME",
    "MESSAGE_OVERHEAD_TOKENS",
    "count_message_tokens",
    "count_messages_tokens",
    "count_tokens",
    "estimate_tokens",
    "get_encoding",
    "reset_encoder_cache",
]
