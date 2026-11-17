"""图片输入校验与多模态消息构造，供各入口共用。"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DATA_URL_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 64
IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
PERSONA_REMINDER_PREFIX = "[人设提醒]"
TIME_CONTEXT_PREFIX = "[时间上下文]"
_DISPLAY_ANNOTATION_PREFIXES = (PERSONA_REMINDER_PREFIX, TIME_CONTEXT_PREFIX)
_WEEKDAYS = "一二三四五六日"


def image_media_type(data: bytes) -> str:
    """按文件签名判断格式，不信任扩展名或客户端声明的 MIME。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("图片格式不支持，请使用 PNG、JPEG、GIF 或 WebP。")


class ImageInput(BaseModel):
    """内嵌图片；只接受 data URL，避免让模型或服务端访问任意外部地址。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data_url: str = Field(max_length=MAX_DATA_URL_CHARS, repr=False)

    @field_validator("data_url")
    @classmethod
    def validate_data_url(cls, value: str) -> str:
        header, separator, encoded = value.partition(",")
        if not separator or header not in {f"data:{mime};base64" for mime in IMAGE_MEDIA_TYPES}:
            raise ValueError("图片必须是 PNG、JPEG、GIF 或 WebP 的 base64 data URL。")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("图片 base64 数据无效。") from exc
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("单张图片不能为空且不能超过 5 MiB。")
        if header != f"data:{image_media_type(data)};base64":
            raise ValueError("图片实际格式与声明的 MIME 类型不一致。")
        return value

    @classmethod
    def from_bytes(cls, data: bytes) -> ImageInput:
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("单张图片不能为空且不能超过 5 MiB。")
        mime = image_media_type(data)
        encoded = base64.b64encode(data).decode("ascii")
        return cls(data_url=f"data:{mime};base64,{encoded}")


def human_message(text: str, images: Sequence[ImageInput] = ()) -> HumanMessage:
    """构造 LangChain 多模态消息；纯文字保持原有字符串形式。"""
    if not images:
        return HumanMessage(content=text)
    if len(images) > MAX_IMAGES:
        raise ValueError(f"每条消息最多支持 {MAX_IMAGES} 张图片。")
    content: list[str | dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend({"type": "image_url", "image_url": {"url": item.data_url}} for item in images)
    return HumanMessage(content=content)


def image_urls(content: str | list[Any]) -> list[str]:
    """提取已验证的内嵌图片，给历史页面展示；不返回远程 URL。"""
    if not isinstance(content, list):
        return []
    urls: list[str] = []
    prefixes = tuple(f"data:{mime};base64," for mime in IMAGE_MEDIA_TYPES)
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "image_url":
            continue
        image = block.get("image_url")
        url = image.get("url") if isinstance(image, dict) else image
        if isinstance(url, str) and url.startswith(prefixes):
            urls.append(url)
    return urls


@cache
def _marker_patterns(
    prefix: str,
) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    escaped = re.escape(prefix)
    return (
        re.compile(rf"(?:^|\r?\n){escaped}(?:\s|$)"),
        re.compile(rf"(?:\r?\n)*{escaped}(?:[ \t][^\r\n]*)?\s*$"),
        re.compile(rf"(?:^|\r?\n){escaped}(?:[ \t][^\r\n]*)?(?=\r?\n|$)"),
    )


def _resolve_zone(name: str | None) -> ZoneInfo:
    candidate = (name or "").strip() or "UTC"
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _has_marker(message: BaseMessage, prefix: str) -> bool:
    line_re, _, _ = _marker_patterns(prefix)
    content = message.content
    if isinstance(content, str):
        return bool(line_re.search(content))
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and bool(line_re.search(str(block.get("text", ""))))
            for block in content
        )
    return False


def _append_marker(message: HumanMessage, prefix: str, body: str) -> HumanMessage:
    cleaned = normalize_persona_reminder(body)
    if not cleaned or _has_marker(message, prefix):
        return message

    note = f"{prefix} {cleaned}"
    content = message.content
    updated: str | list[str | dict[Any, Any]]
    if isinstance(content, str):
        updated = f"{content}\n\n{note}" if content else note
    elif isinstance(content, list):
        updated = [*content, {"type": "text", "text": note}]
    else:
        return message
    return message.model_copy(update={"content": updated})


def _strip_markers_text(text: str, prefixes: Sequence[str]) -> str:
    cleaned = text
    # 连续剥离直到末尾不再带有任意内部标记（兼容「[时间上下文] + [人设提醒]」并存）。
    while True:
        before = cleaned
        for prefix in prefixes:
            if prefix not in cleaned:
                continue
            _, trailing_re, inline_re = _marker_patterns(prefix)
            cleaned = trailing_re.sub("", cleaned)
            if prefix in cleaned:
                cleaned = inline_re.sub("", cleaned).lstrip("\r\n")
        if cleaned == before:
            return cleaned


def _strip_markers_message(message: BaseMessage, prefixes: Sequence[str]) -> BaseMessage:
    if not isinstance(message, HumanMessage):
        return message

    content = message.content
    if isinstance(content, str):
        cleaned = _strip_markers_text(content, prefixes)
        if cleaned == content:
            return message
        return message.model_copy(update={"content": cleaned})

    if isinstance(content, list):
        changed = False
        new_blocks: list[str | dict[Any, Any]] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                raw = block.get("text")
                if isinstance(raw, str) and any(prefix in raw for prefix in prefixes):
                    stripped = _strip_markers_text(raw, prefixes)
                    changed = True
                    if not stripped:
                        continue
                    new_blocks.append({**block, "text": stripped})
                    continue
            new_blocks.append(block)
        if changed:
            return message.model_copy(update={"content": new_blocks})
    return message


def normalize_persona_reminder(reminder: str) -> str:
    """将人设提醒等内部锚点折叠为单行文本，方便稳定附着与展示剥离。"""
    return " ".join(reminder.split())


def has_persona_reminder(message: BaseMessage) -> bool:
    """判断消息是否已包含人设提醒锚点。"""
    return _has_marker(message, PERSONA_REMINDER_PREFIX)


def should_attach_persona_reminder(
    history: Sequence[BaseMessage],
    *,
    interval: int,
    has_summary: bool = False,
) -> bool:
    """判断本轮新用户消息是否需要持久化附着人设锚点。

    按固定轮次间隔插入并保留在历史中，使后续轮次无需改写任何历史前缀，
    保证 Prefix Cache 100% 命中；若摘要压缩裁掉了先前的所有锚点，则在压缩后的首轮补回。
    """
    if interval <= 0:
        return False

    turns_since_anchor = 0
    saw_anchor = False
    for message in history:
        if not isinstance(message, HumanMessage):
            continue
        if has_persona_reminder(message):
            turns_since_anchor = 0
            saw_anchor = True
        else:
            turns_since_anchor += 1

    if has_summary and not saw_anchor:
        return True
    return (turns_since_anchor + 1) >= interval


def append_persona_reminder(message: HumanMessage, reminder: str) -> HumanMessage:
    """将人设锚点附着到用户消息末尾，返回新消息副本；已附着或提醒为空时原样返回。"""
    return _append_marker(message, PERSONA_REMINDER_PREFIX, reminder)


def build_time_context(
    *,
    last_active_at: datetime | None,
    timezone_name: str,
    gap_hours: float,
    now: datetime | None = None,
) -> str:
    """跨天或长时间中断后，生成附在当轮用户消息上的轻量时间上下文。

    只在继续已有会话且确实跨天或超过 ``gap_hours`` 时返回正文；同日短间隔内
    返回空串，避免破坏 Prefix Cache。
    """
    if gap_hours <= 0 or last_active_at is None:
        return ""
    zone = _resolve_zone(timezone_name)
    current = (now or datetime.now(UTC)).astimezone(zone)
    previous = (
        last_active_at.replace(tzinfo=UTC) if last_active_at.tzinfo is None else last_active_at
    ).astimezone(zone)
    elapsed_seconds = (current - previous).total_seconds()
    if elapsed_seconds <= 0:
        return ""
    crossed_day = current.date() != previous.date()
    if not crossed_day and elapsed_seconds < gap_hours * 3600:
        return ""

    days_diff = (current.date() - previous.date()).days
    if days_diff >= 2:
        gap_note = f"距上次对话已过 {days_diff} 天"
    elif days_diff == 1:
        gap_note = "距上次对话已跨天（昨天）"
    else:
        hours = max(1, int(elapsed_seconds // 3600))
        gap_note = f"距上次对话已过约 {hours} 小时"

    weekday = _WEEKDAYS[current.weekday()]
    tz_label = current.tzname() or ""
    stamp = f"{current:%Y-%m-%d %H:%M} {tz_label}".strip()
    return (
        f"当前时间：{stamp}（周{weekday}），{gap_note}；"
        "历史中的「今天/昨天/明天」均相对当时而言，请勿沿用旧日期。"
    )


def append_time_context(message: HumanMessage, time_context: str) -> HumanMessage:
    """将时间上下文锚点附着到用户消息末尾，返回新消息副本。"""
    return _append_marker(message, TIME_CONTEXT_PREFIX, time_context)


def strip_persona_reminder_text(text: str) -> str:
    """仅从文本中移除人设锚点标记（保留时间上下文，供历史摘要识别日期）。"""
    return _strip_markers_text(text, (PERSONA_REMINDER_PREFIX,))


def strip_display_annotations_text(text: str) -> str:
    """从展示文本中移除人设锚点与时间上下文标记，不影响原始业务内容。"""
    return _strip_markers_text(text, _DISPLAY_ANNOTATION_PREFIXES)


def strip_display_annotations_message(message: BaseMessage) -> BaseMessage:
    """从历史消息中移除内部锚点与时间上下文标记以便面向用户展示。"""
    return _strip_markers_message(message, _DISPLAY_ANNOTATION_PREFIXES)
