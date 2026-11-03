"""图片输入的格式、大小、消息构造和 token 预算。"""

from __future__ import annotations

import base64

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from doudou.agent.context import HistorySummarizer
from doudou.agent.messages import MAX_IMAGE_BYTES, ImageInput, human_message, image_urls
from doudou.api.schemas.chat import ChatRequest
from doudou.utils.tokens import IMAGE_ESTIMATE_TOKENS, count_message_tokens

PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2ioAAAAASUVORK5CYII="
)
PNG_BYTES = base64.b64decode(PNG_BASE64)
PNG_URL = f"data:image/png;base64,{PNG_BASE64}"


def test_image_message_preserves_text_and_binary() -> None:
    image = ImageInput.from_bytes(PNG_BYTES)
    message = human_message("这是什么？", [image])
    assert message.content == [
        {"type": "text", "text": "这是什么？"},
        {"type": "image_url", "image_url": {"url": PNG_URL}},
    ]
    assert image_urls(message.content) == [PNG_URL]
    assert human_message("纯文字").content == "纯文字"
    assert human_message("", [image]).content == [
        {"type": "image_url", "image_url": {"url": PNG_URL}}
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/image.png",
        "file:///etc/passwd",
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:image/png;base64,%%%",
        "data:image/png;base64,",
        "data:image/png;base64,dGV4dA==",
        f"data:image/jpeg;base64,{PNG_BASE64}",
    ],
)
def test_invalid_images_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        ImageInput(data_url=url)


def test_oversized_images_and_excessive_count_are_rejected() -> None:
    with pytest.raises(ValueError, match="5 MiB"):
        ImageInput.from_bytes(PNG_BYTES + b"x" * MAX_IMAGE_BYTES)
    image = ImageInput(data_url=PNG_URL)
    with pytest.raises(ValueError, match="最多"):
        human_message("", [image] * 5)
    with pytest.raises(ValidationError):
        ChatRequest(images=[image] * 5)


def test_image_only_request_and_empty_request() -> None:
    assert ChatRequest(images=[ImageInput(data_url=PNG_URL)]).message == ""
    with pytest.raises(ValidationError, match="文字或发送图片"):
        ChatRequest(message="  ")


def test_image_tokens_do_not_depend_on_base64_length() -> None:
    short = HumanMessage(content=[{"type": "image_url", "image_url": {"url": PNG_URL}}])
    long = HumanMessage(content=[{"type": "image_url", "image_url": {"url": PNG_URL * 10000}}])
    plain = HumanMessage(content="")
    assert count_message_tokens(short) == count_message_tokens(long)
    assert count_message_tokens(short) - count_message_tokens(plain) == IMAGE_ESTIMATE_TOKENS
    transcript = HistorySummarizer._render([human_message("看图", [ImageInput(data_url=PNG_URL)])])
    assert "看图" in transcript
    assert "image_url" in transcript
    assert PNG_BASE64 not in transcript
