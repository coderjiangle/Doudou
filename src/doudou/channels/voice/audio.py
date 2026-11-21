"""Audio framing, WAV encoding and sounddevice microphone/speaker wrappers."""

from __future__ import annotations

import asyncio
import contextlib
import io
import queue
import threading
import wave
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from doudou.channels.voice.errors import VoiceError
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

INPUT_SAMPLE_RATE = 16000
VAD_WINDOW_SAMPLES = 512
TTS_SAMPLE_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2  # 16-bit


class AudioInputStream(Protocol):
    """16 kHz 单声道 float32 音频帧输入流协议（便于离线单测注入）。"""

    async def start(self) -> None: ...

    async def read_chunk(self) -> npt.NDArray[np.float32] | None: ...

    def drain(self) -> int: ...

    async def stop(self) -> None: ...


class AudioPlayer(Protocol):
    """24 kHz 单声道 16-bit PCM 音频播放器协议（便于离线单测注入）。"""

    async def start(self) -> None: ...

    async def write_pcm(self, pcm_bytes: bytes) -> None: ...

    async def finish(self) -> None: ...

    async def abort(self) -> None: ...


def float32_to_pcm16_bytes(samples: npt.NDArray[np.float32]) -> bytes:
    """把 ``[-1.0, 1.0]`` 的 float32 采样转成 16-bit little-endian PCM 字节。"""
    clipped = np.clip(samples, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype("<i2")
    return pcm16.tobytes()


def float32_to_wav_bytes(
    samples: npt.NDArray[np.float32], *, sample_rate: int = INPUT_SAMPLE_RATE
) -> bytes:
    """把 float32 单声道音频编码为 16-bit PCM WAV 字节，供 DeepSeek ASR 上传。"""
    pcm = float32_to_pcm16_bytes(samples)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(PCM_CHANNELS)
        wf.setsampwidth(PCM_SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buffer.getvalue()


def pcm16_bytes_to_float32(pcm_bytes: bytes) -> npt.NDArray[np.float32]:
    """把 16-bit PCM 字节解码为 ``[-1.0, 1.0]`` float32 数组。"""
    if not pcm_bytes:
        return np.zeros(0, dtype=np.float32)
    arr = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)
    return arr / 32768.0


class SoundDeviceMic:
    """基于 ``sounddevice.InputStream`` 的 16 kHz 单声道麦克风采集器。"""

    def __init__(
        self,
        *,
        sample_rate: int = INPUT_SAMPLE_RATE,
        chunk_samples: int = VAD_WINDOW_SAMPLES,
        device: int | str | None = None,
        max_queue_chunks: int = 200,
    ) -> None:
        self.sample_rate = sample_rate
        self.chunk_samples = chunk_samples
        self.device = device
        self._max_queue_chunks = max_queue_chunks
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[npt.NDArray[np.float32] | None] | None = None
        self._stream: Any | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        try:
            import sounddevice as sd
        except Exception as exc:
            raise VoiceError(f"无法加载 sounddevice/PortAudio：{exc}") from exc

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self._max_queue_chunks)
        self._running = True

        def _callback(
            indata: npt.NDArray[np.float32],
            _frames: int,
            _time_info: Any,
            status: Any,
        ) -> None:
            if status:
                logger.debug("voice.mic_status", status=str(status))
            if not self._running or self._loop is None or self._queue is None:
                return
            chunk = np.array(indata[:, 0], dtype=np.float32, copy=True)

            def _enqueue() -> None:
                if self._queue is None:
                    return
                if self._queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        self._queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait(chunk)

            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(_enqueue)

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=PCM_CHANNELS,
                dtype="float32",
                blocksize=self.chunk_samples,
                device=self.device,
                callback=_callback,
            )
            self._stream.start()
        except Exception as exc:
            self._running = False
            raise VoiceError(f"无法打开麦克风输入设备：{exc}") from exc
        logger.info("voice.mic_started", sample_rate=self.sample_rate, device=self.device)

    async def read_chunk(self) -> npt.NDArray[np.float32] | None:
        if self._queue is None or not self._running:
            return None
        return await self._queue.get()

    def drain(self) -> int:
        """丢弃队列中尚未消费的旧音频帧（TTS 播报结束后防自激）。"""
        if self._queue is None:
            return 0
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        return dropped

    async def stop(self) -> None:
        self._running = False
        stream = self._stream
        self._stream = None
        if stream is not None:
            await asyncio.to_thread(_close_sd_stream, stream)
        if self._queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
        logger.info("voice.mic_stopped")


class SoundDeviceSpeaker:
    """基于 ``sounddevice.RawOutputStream`` 的流式 24 kHz PCM 音箱播放器。"""

    def __init__(
        self,
        *,
        sample_rate: int = TTS_SAMPLE_RATE,
        device: int | str | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._abort_event = threading.Event()
        self._error: Exception | None = None

    async def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._abort_event.clear()
        self._error = None
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run_worker, daemon=True)
        self._thread.start()

    def _run_worker(self) -> None:
        stream: Any | None = None
        try:
            import sounddevice as sd

            stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=PCM_CHANNELS,
                dtype="int16",
                device=self.device,
            )
            stream.start()
            while not self._abort_event.is_set():
                chunk = self._queue.get()
                if chunk is None:
                    break
                if chunk and not self._abort_event.is_set():
                    stream.write(chunk)
        except Exception as exc:
            self._error = exc
            logger.warning("voice.speaker_error", error=str(exc))
        finally:
            if stream is not None:
                _close_sd_stream(stream)

    async def write_pcm(self, pcm_bytes: bytes) -> None:
        if pcm_bytes and not self._abort_event.is_set():
            self._queue.put(pcm_bytes)

    async def finish(self) -> None:
        self._queue.put(None)
        thread = self._thread
        self._thread = None
        if thread is not None:
            await asyncio.to_thread(thread.join, 30.0)
        if self._error is not None:
            raise VoiceError(f"音箱播放失败：{self._error}") from self._error

    async def abort(self) -> None:
        self._abort_event.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._queue.put(None)
        thread = self._thread
        self._thread = None
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)


def _close_sd_stream(stream: Any) -> None:
    with contextlib.suppress(Exception):
        stream.stop()
    with contextlib.suppress(Exception):
        stream.close()


__all__ = [
    "INPUT_SAMPLE_RATE",
    "PCM_CHANNELS",
    "PCM_SAMPLE_WIDTH",
    "TTS_SAMPLE_RATE",
    "VAD_WINDOW_SAMPLES",
    "AudioInputStream",
    "AudioPlayer",
    "SoundDeviceMic",
    "SoundDeviceSpeaker",
    "VoiceError",
    "float32_to_pcm16_bytes",
    "float32_to_wav_bytes",
    "pcm16_bytes_to_float32",
]
