"""Text truncation, chunking and markdown helpers."""

from __future__ import annotations

import json
from html.parser import HTMLParser
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


# HTML → 纯文本时直接丢弃内容的标签。
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})
# 这些标签在纯文本里代表换行，保留它能保住原文的段落结构。
_BLOCK_TAGS = frozenset(
    {
        "br",
        "p",
        "div",
        "li",
        "tr",
        "td",
        "th",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)


class _TextExtractor(HTMLParser):
    """把 HTML 抽成文本：丢掉 script/style，块级标签折成换行。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        # 先按行折叠行内空白，再丢掉空行 —— 直接 collapse 全部空白会把段落拉成一行。
        lines = (" ".join(line.split()) for line in raw.splitlines())
        return "\n".join(line for line in lines if line)


def html_to_text(html: str, *, limit: int | None = None) -> str:
    """把 HTML 粗略转成纯文本。

    目标是「别把整页标签塞进模型上下文」，不是精确还原排版 ——
    因此只做标签剥离、空白折叠与 script/style 剔除。
    解析失败时退回原文（截断），宁可难看也不要丢内容。
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, AssertionError):
        return truncate(html, limit) if limit is not None else html
    text = parser.text()
    return truncate(text, limit) if limit is not None else text


__all__ = [
    "content_to_text",
    "html_to_text",
    "stringify_tool_result",
    "truncate",
]
