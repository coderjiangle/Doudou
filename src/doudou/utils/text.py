"""Text truncation, chunking and markdown helpers."""

from __future__ import annotations

import json
from typing import Any

_DEFAULT_SUFFIX = "…"


def content_to_text(content: str | list[Any] | None) -> str:
    """把 LangChain 消息的 ``content`` 归一化成纯文本。

    ``content`` 可能是字符串，也可能是内容块列表（如
    ``[{"type": "text", "text": "..."}]``，或 Anthropic 风格的 dict 块）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def truncate(text: str, limit: int, *, suffix: str = _DEFAULT_SUFFIX) -> str:
    """按字符数截断文本，并在结尾标注被截断的长度。"""
    if limit <= 0 or len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}{suffix}（已截断 {dropped} 字）"


def stringify_tool_result(result: Any, *, limit: int | None = None) -> str:
    """把工具返回值转成可回填给模型的字符串。

    - ``str`` 直接返回；
    - 其它类型走 ``json.dumps``，无法序列化时回退到 ``str()``。
    """
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(result)
    return truncate(text, limit) if limit is not None else text


__all__ = ["content_to_text", "stringify_tool_result", "truncate"]
