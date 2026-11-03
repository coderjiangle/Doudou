"""Pydantic models of the Feishu event and message payloads.

lark-oapi 已经提供了带类型的对象，这里只建模**我们自己需要解析的部分**：
消息内容（``content`` 是一段 JSON 字符串）与卡片回调载荷。
这样解析逻辑可以脱离 lark 对象单独测试。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TextContent(BaseModel):
    """``message_type == "text"`` 的 content。"""

    model_config = ConfigDict(extra="ignore")

    text: str = ""


class PostNode(BaseModel):
    """``message_type == "post"``（富文本）里的一个节点。"""

    model_config = ConfigDict(extra="ignore")

    tag: str = ""
    text: str = ""
    href: str = ""
    user_id: str = ""
    #: ``tag == "a"`` 时是链接，``tag == "at"`` 时是 @ 某人。
    content: Any | None = None


class CardActionValue(BaseModel):
    """卡片按钮的 ``value`` 字段。

    我们自己在按钮里塞的结构：``{"action": "approve", "approval_id": "ap-0001-…"}``。
    人工确认靠它把「点了哪个按钮、针对哪一次确认」传回来。

    ``approval_id`` 而不是工具名：点头只授权**这一次**具体的调用 ——
    把「同意 write_file」当成一次性授权会让后续每次写文件都静默通过。
    """

    model_config = ConfigDict(extra="ignore")

    action: str = ""
    #: 确认中介分配的 ID，决定要落哪一个待决请求。
    approval_id: str = ""
    call_id: str = ""
    thread_id: str = ""


class ParsedContent(BaseModel):
    """归一化后的消息内容。"""

    text: str = ""
    #: 富文本里的链接，按出现顺序。检索类回答常常需要它们。
    links: list[str] = Field(default_factory=list)
    image_keys: list[str] = Field(default_factory=list)


def parse_content(message_type: str, raw: str | None) -> ParsedContent:
    """把飞书消息的 ``content`` 解析成纯文本。

    飞书把 content 编码成一段 JSON 字符串，且不同 ``message_type`` 结构不同：

    * ``text``  —— ``{"text": "..."}``
    * ``post``  —— ``{"title": "...", "content": [[{tag, text}, ...], ...]}``
    * ``image`` —— ``{"image_key": "img_..."}``。
    * 其它（file / audio …）—— 暂不支持，返回空文本。
    """
    if not raw:
        return ParsedContent()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        # 解析不了就当纯文本用：宁可把原文交给模型，也不要丢掉用户说的话。
        return ParsedContent(text=raw.strip())
    if not isinstance(payload, dict):
        return ParsedContent()

    if message_type == "text":
        return ParsedContent(text=str(payload.get("text") or "").strip())

    if message_type == "post":
        return _parse_post(payload)

    if message_type == "image":
        key = payload.get("image_key")
        return ParsedContent(image_keys=[key] if isinstance(key, str) and key else [])

    return ParsedContent()


def _parse_post(payload: dict[str, Any]) -> ParsedContent:
    """把富文本拍平成文本，并保留链接与图片；兼容带语言键的消息。"""
    if "content" not in payload:
        for language in ("zh_cn", "en_us", *payload):
            localized = payload.get(language)
            if isinstance(localized, dict) and "content" in localized:
                return _parse_post(localized)
    lines: list[str] = []
    links: list[str] = []
    image_keys: list[str] = []
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        lines.append(title.strip())

    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return ParsedContent(text="\n".join(lines), links=links)

    for block in blocks:
        if not isinstance(block, list):
            continue
        parts: list[str] = []
        for node in block:
            if not isinstance(node, dict):
                continue
            tag = str(node.get("tag") or "")
            if tag == "text":
                parts.append(str(node.get("text") or ""))
            elif tag == "a":
                href = str(node.get("href") or "")
                parts.append(str(node.get("text") or href))
                if href:
                    links.append(href)
            elif tag == "at":
                # @ 某人在纯文本里保留为 @名字 —— 群聊里这说明「这条是给我说的」。
                parts.append(f"@{node.get('user_id') or ''}")
            elif tag == "img":
                key = node.get("image_key")
                if isinstance(key, str) and key and key not in image_keys:
                    image_keys.append(key)
        line = "".join(parts).strip()
        if line:
            lines.append(line)

    return ParsedContent(text="\n".join(lines), links=links, image_keys=image_keys)


__all__ = [
    "CardActionValue",
    "ParsedContent",
    "PostNode",
    "TextContent",
    "parse_content",
]
