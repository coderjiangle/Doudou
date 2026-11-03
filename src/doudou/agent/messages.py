"""图片输入校验与多模态消息构造，供各入口共用。"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DATA_URL_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 64
IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


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
