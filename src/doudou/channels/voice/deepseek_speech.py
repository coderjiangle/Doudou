"""DeepSeek ASR (HTTP) and TTS (WebSocket streaming + HTTP) clients."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from doudou.channels.voice.errors import VoiceError
from doudou.observability.logging import get_logger
from doudou.utils.async_utils import retry_async

logger = get_logger(__name__)

MAX_ASR_BYTES = 8 * 1024 * 1024  # DeepSeek ASR 单文件上限 8 MiB
_LEADING_STRIP = " \t\r\n，。！？、：；,.!?:;\"'“”‘’「」『』（）()【】[]"


@dataclass(frozen=True, slots=True)
class AsrResult:
    """DeepSeek ASR 返回结果。"""

    text: str
    duration_ms: int = 0
    request_id: str = ""


def strip_wake_prefix(text: str, wake_words: Sequence[str]) -> tuple[bool, str]:
    """检查文本是否以唤醒词开头，并返回剥离唤醒词后的指令正文。"""
    cleaned = text.strip().lstrip(_LEADING_STRIP)
    if not cleaned:
        return False, ""

    lowered = cleaned.lower()
    ordered = sorted((word.strip() for word in wake_words if word.strip()), key=len, reverse=True)
    for word in ordered:
        if lowered.startswith(word.lower()):
            remainder = cleaned[len(word) :].lstrip(_LEADING_STRIP)
            return True, remainder
    return False, cleaned


class DeepSeekAsrClient:
    """调用 ``POST /v1/tools/asr`` 将 WAV 音频转写为文本。"""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = "https://api.deepseek.com/v1/tools/asr",
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self._client = client

    async def transcribe_wav(self, wav_bytes: bytes) -> AsrResult:
        if not wav_bytes:
            raise VoiceError("音频内容为空，无法识别。")
        if len(wav_bytes) > MAX_ASR_BYTES:
            raise VoiceError("单段语音超过 DeepSeek ASR 8 MiB 上限，已放弃上传。")

        headers = {"Authorization": f"Bearer {self.api_key}"}

        async def _call() -> httpx.Response:
            files = {"file": ("speech.wav", wav_bytes, "audio/wav")}
            if self._client is not None:
                resp = await self._client.post(
                    self.endpoint,
                    headers=headers,
                    files=files,
                    timeout=self.timeout,
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as owned:
                    resp = await owned.post(self.endpoint, headers=headers, files=files)
            if resp.status_code >= 500 or resp.status_code == 429:
                resp.raise_for_status()
            return resp

        try:
            response = await retry_async(
                _call,
                attempts=2,
                retry_on=(httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError),
                label="deepseek_asr",
            )
        except Exception as exc:
            raise VoiceError(f"DeepSeek 语音识别请求失败：{exc}") from exc

        if response.status_code >= 400:
            raise VoiceError(f"DeepSeek 语音识别返回 {response.status_code}：{response.text[:300]}")

        payload = response.json()
        result = AsrResult(
            text=str(payload.get("text") or "").strip(),
            duration_ms=int(payload.get("duration_ms") or 0),
            request_id=str(payload.get("request_id") or ""),
        )
        logger.info(
            "voice.asr_done",
            duration_ms=result.duration_ms,
            chars=len(result.text),
            request_id=result.request_id,
        )
        return result


WsConnectFactory = Callable[..., Any]


class DeepSeekTtsClient:
    """调用 DeepSeek TTS（WebSocket 增量流式为主，兼容 HTTP 整段合成）。"""

    def __init__(
        self,
        *,
        api_key: str,
        ws_url: str = "wss://api.deepseek.com/v1/tools/tts",
        http_url: str = "https://api.deepseek.com/v1/tools/tts",
        voice_id: str = "stella",
        filter_markdown: bool = True,
        timeout: float = 60.0,
        ws_connect: WsConnectFactory | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.ws_url = ws_url
        self.http_url = http_url
        self.voice_id = voice_id
        self.filter_markdown = filter_markdown
        self.timeout = timeout
        self._ws_connect = ws_connect
        self._http_client = http_client

    def build_ws_url(self, *, audio_format: str = "pcm") -> str:
        query = urlencode(
            {
                "voice_id": self.voice_id,
                "format": audio_format,
                "filter_markdown": str(self.filter_markdown).lower(),
            }
        )
        sep = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{sep}{query}"

    async def stream_synthesize(
        self,
        text_chunks: AsyncIterator[str],
        on_audio: Callable[[bytes], Awaitable[None]],
    ) -> None:
        """把流式文本实时发往 DeepSeek TTS WebSocket，并逐帧回调 24 kHz PCM 字节。"""
        url = self.build_ws_url(audio_format="pcm")
        headers = {"Authorization": f"Bearer {self.api_key}"}

        if self._ws_connect is not None:
            cm = self._ws_connect(url, additional_headers=headers)
        else:
            import websockets

            cm = websockets.connect(url, additional_headers=headers, open_timeout=self.timeout)

        async with cm as ws:

            async def _send_loop() -> None:
                sent_any = False
                async for chunk in text_chunks:
                    if not chunk:
                        continue
                    sent_any = True
                    await ws.send(json.dumps({"event": "text", "text": chunk}))
                if not sent_any:
                    await ws.send(json.dumps({"event": "text", "text": " "}))
                await ws.send(json.dumps({"event": "finish_text"}))

            async def _recv_loop() -> None:
                async for message in ws:
                    if isinstance(message, (bytes, bytearray)):
                        await on_audio(bytes(message))
                        continue
                    payload = json.loads(message)
                    if payload.get("event") == "finish":
                        code = str(payload.get("code") or "")
                        if code != "success":
                            msg = str(payload.get("message") or code)
                            raise VoiceError(f"DeepSeek TTS 合成失败（{code}）：{msg}")
                        return

            sender = asyncio.create_task(_send_loop())
            try:
                await _recv_loop()
                await sender
            except Exception:
                sender.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await sender
                raise

    async def synthesize_pcm(self, text: str) -> bytes:
        """HTTP 整段合成 24 kHz 16-bit PCM（适合短提示音或离线回退）。"""
        cleaned = text.strip()
        if not cleaned:
            return b""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "text": cleaned,
            "voice_id": self.voice_id,
            "format": "pcm",
            "filter_markdown": self.filter_markdown,
        }

        async def _call() -> httpx.Response:
            if self._http_client is not None:
                resp = await self._http_client.post(
                    self.http_url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout,
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as owned:
                    resp = await owned.post(self.http_url, headers=headers, json=body)
            if resp.status_code >= 500 or resp.status_code == 429:
                resp.raise_for_status()
            return resp

        response = await retry_async(
            _call,
            attempts=2,
            retry_on=(httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError),
            label="deepseek_tts",
        )
        if response.status_code >= 400:
            raise VoiceError(f"DeepSeek TTS 返回 {response.status_code}：{response.text[:300]}")
        return bytes(response.content)


__all__ = [
    "MAX_ASR_BYTES",
    "AsrResult",
    "DeepSeekAsrClient",
    "DeepSeekTtsClient",
    "strip_wake_prefix",
]
