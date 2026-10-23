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
    **非文本块被忽略**：这个函数服务于「模型输出的增量文本」，
    那里不会有图片；工具结果请用 :func:`stringify_tool_result`。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return blocks_to_text(content, placeholders=False)


#: LangChain 认识的内容块类型。
#:
#: 判据必须用**白名单**而不是「有 type 字段就行」：普通 API 数据里
#: ``{"type": "invoice", "amount": 100}`` 太常见了，那种情况一旦被误判成内容块，
#: 结果会被改写成 ``[invoice 内容]`` —— 静默丢失数据，而且很难联想到原因。
CONTENT_BLOCK_TYPES = frozenset(
    {
        "text",
        "image",
        "image_url",
        "audio",
        "input_audio",
        "file",
        "video",
        "document",
        "resource",
        "resource_link",
        "tool_use",
        "tool_result",
        "thinking",
        "reasoning",
    }
)


def looks_like_content_blocks(value: Any) -> bool:
    """是否是「内容块列表」。

    判据：非空列表，且每一项都是带**已知** ``type`` 的 dict。
    刻意保守 —— 误判的代价是静默改写工具结果，比漏判严重得多。
    """
    if not isinstance(value, list) or not value:
        return False
    return all(
        isinstance(item, dict) and str(item.get("type") or "") in CONTENT_BLOCK_TYPES
        for item in value
    )


def blocks_to_text(blocks: list[Any], *, placeholders: bool = True) -> str:
    """把内容块列表拍平成文本。

    Args:
        blocks: ``[{"type": "text", "text": "..."}, ...]``。
        placeholders: 非文本块（图片、音频、资源）是否用占位标记代替。
            ``True`` 用于工具结果：直接丢掉会让模型完全察觉不到
            「这里本来有内容」；``False`` 用于模型输出的增量文本。
    """
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
            continue
        block_type = ""
        text = ""
        if isinstance(block, dict):
            block_type = str(block.get("type") or "")
            raw = block.get("text")
            text = raw if isinstance(raw, str) else ""
        else:
            block_type = str(getattr(block, "type", "") or "")
            raw = getattr(block, "text", None)
            text = raw if isinstance(raw, str) else ""

        if text:
            parts.append(text)
        elif block_type and placeholders:
            parts.append(_block_placeholder(block_type))
    # 块之间用空行分隔：MCP 工具返回的多个块通常是不同段落。
    return "\n\n".join(parts)


def _block_placeholder(block_type: str) -> str:
    mapping = {
        "image": "[图片内容，当前无法作为文本使用]",
        "audio": "[音频内容，当前无法作为文本使用]",
        "resource": "[资源引用]",
        "resource_link": "[资源链接]",
    }
    return mapping.get(block_type, f"[{block_type} 内容]")


def stringify_tool_result(result: Any, *, limit: int | None = None) -> str:
    """把工具返回值转成可回填给模型的字符串。

    三种情况：

    - ``str`` 直接返回；
    - **内容块列表**（MCP 工具常见）拍平成纯文本 —— 默认的
      ``[{"type": "text", "text": "..."}]`` 包装既白花 token，
      其中的内部 id 还会诱导模型把它当成有效引用；
    - 其它类型走 ``json.dumps``，无法序列化时回退到 ``str()``。
    """
    if isinstance(result, str):
        text = result
    elif looks_like_content_blocks(result):
        text = blocks_to_text(result)
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(result)
    return truncate(text, limit) if limit is not None else text


def truncate(text: str, limit: int, *, suffix: str = _DEFAULT_SUFFIX) -> str:
    """按字符数截断文本，并在结尾标注被截断的长度。"""
    if limit <= 0 or len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}{suffix}（已截断 {dropped} 字）"


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
    "blocks_to_text",
    "content_to_text",
    "html_to_text",
    "looks_like_content_blocks",
    "stringify_tool_result",
    "truncate",
]
