"""Local streaming wake-word + Silero VAD command segmentation state machine."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from doudou.channels.voice.audio import INPUT_SAMPLE_RATE, VAD_WINDOW_SAMPLES
from doudou.channels.voice.models import VoiceModelPaths
from doudou.observability.logging import get_logger

logger = get_logger(__name__)


class DetectorState(StrEnum):
    IDLE = "idle"
    CAPTURING = "capturing"


@dataclass(frozen=True, slots=True)
class CapturedUtterance:
    """一次唤醒后截取到的完整语音指令。"""

    wake_word: str
    samples: npt.NDArray[np.float32]


class KwsEngine(Protocol):
    """流式唤醒词识别引擎协议。"""

    def accept_chunk(self, chunk: npt.NDArray[np.float32]) -> str: ...

    def reset(self) -> None: ...


class VadEngine(Protocol):
    """流式语音活动检测与断句引擎协议。"""

    def accept_waveform(self, chunk: npt.NDArray[np.float32]) -> None: ...

    def is_speech_detected(self) -> bool: ...

    def empty(self) -> bool: ...

    def pop_segment(self) -> npt.NDArray[np.float32]: ...

    def flush(self) -> None: ...

    def reset(self) -> None: ...


class SherpaKwsEngine:
    """基于 ``sherpa_onnx.KeywordSpotter`` 的本地唤醒词引擎。"""

    def __init__(
        self,
        paths: VoiceModelPaths,
        *,
        keywords_file: Path | None = None,
        sample_rate: int = INPUT_SAMPLE_RATE,
        keywords_score: float = 1.5,
        keywords_threshold: float = 0.25,
        num_threads: int = 1,
    ) -> None:
        import sherpa_onnx

        self.sample_rate = sample_rate
        kw_path = keywords_file or paths.kws_keywords
        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=str(paths.kws_tokens),
            encoder=str(paths.kws_encoder),
            decoder=str(paths.kws_decoder),
            joiner=str(paths.kws_joiner),
            keywords_file=str(kw_path),
            num_threads=num_threads,
            keywords_score=keywords_score,
            keywords_threshold=keywords_threshold,
            provider="cpu",
        )
        self._stream: Any = self._kws.create_stream()

    def accept_chunk(self, chunk: npt.NDArray[np.float32]) -> str:
        self._stream.accept_waveform(self.sample_rate, chunk)
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
            keyword = str(self._kws.get_result(self._stream) or "").strip()
            if keyword:
                self._kws.reset_stream(self._stream)
                return keyword
        return ""

    def reset(self) -> None:
        self._kws.reset_stream(self._stream)


class SherpaVadEngine:
    """基于 ``sherpa_onnx.VoiceActivityDetector``（Silero VAD）的断句引擎。"""

    def __init__(
        self,
        paths: VoiceModelPaths,
        *,
        sample_rate: int = INPUT_SAMPLE_RATE,
        threshold: float = 0.5,
        min_silence_duration: float = 0.9,
        min_speech_duration: float = 0.25,
        max_speech_duration: float = 20.0,
        window_size: int = VAD_WINDOW_SAMPLES,
    ) -> None:
        import sherpa_onnx

        config = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=str(paths.vad_model),
                threshold=threshold,
                min_silence_duration=min_silence_duration,
                min_speech_duration=min_speech_duration,
                window_size=window_size,
                max_speech_duration=max_speech_duration,
            ),
            sample_rate=sample_rate,
            num_threads=1,
            provider="cpu",
        )
        self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=60.0)

    def accept_waveform(self, chunk: npt.NDArray[np.float32]) -> None:
        self._vad.accept_waveform(chunk)

    def is_speech_detected(self) -> bool:
        return bool(self._vad.is_speech_detected())

    def empty(self) -> bool:
        return bool(self._vad.empty())

    def pop_segment(self) -> npt.NDArray[np.float32]:
        seg = self._vad.front
        samples = np.asarray(seg.samples, dtype=np.float32)
        self._vad.pop()
        return samples

    def flush(self) -> None:
        self._vad.flush()

    def reset(self) -> None:
        self._vad.reset()


class WakeCommandDetector:
    """双阶段本地检测器：``IDLE`` 仅本地判唤醒词，唤醒后 ``CAPTURING`` 用 VAD 切完一整句。"""

    def __init__(
        self,
        kws: KwsEngine,
        vad: VadEngine,
        *,
        sample_rate: int = INPUT_SAMPLE_RATE,
        pre_roll_seconds: float = 0.8,
        command_wait_seconds: float = 4.0,
        max_record_seconds: float = 20.0,
        on_wake: Callable[[str], None] | None = None,
        on_timeout: Callable[[str], None] | None = None,
    ) -> None:
        self.kws = kws
        self.vad = vad
        self.sample_rate = sample_rate
        self.on_wake = on_wake
        self.on_timeout = on_timeout

        chunk_samples = VAD_WINDOW_SAMPLES
        max_pre_chunks = max(1, round(pre_roll_seconds * sample_rate / chunk_samples))
        self._pre_roll: deque[npt.NDArray[np.float32]] = deque(maxlen=max_pre_chunks)
        self._command_wait_samples = max(chunk_samples, round(command_wait_seconds * sample_rate))
        self._max_record_samples = max(chunk_samples, round(max_record_seconds * sample_rate))

        self.state = DetectorState.IDLE
        self._wake_word = ""
        self._capture_chunks: list[npt.NDArray[np.float32]] = []
        self._captured_samples = 0
        self._saw_speech = False

    def reset(self) -> None:
        """清空缓冲并回到待机监听态（音箱播报结束冷却后调用）。"""
        self.state = DetectorState.IDLE
        self._wake_word = ""
        self._pre_roll.clear()
        self._capture_chunks.clear()
        self._captured_samples = 0
        self._saw_speech = False
        self.kws.reset()
        self.vad.reset()

    def force_wake(self, wake_word: str) -> None:
        """外部打断后直接切入录音采集态，省去再次喊唤醒词。"""
        self._pre_roll.clear()
        self._enter_capturing(wake_word)

    def feed(self, chunk: npt.NDArray[np.float32]) -> CapturedUtterance | None:
        """喂入一帧 16 kHz float32 音频；一句话说完时返回 :class:`CapturedUtterance`。"""
        if chunk.size == 0:
            return None

        if self.state is DetectorState.IDLE:
            self._pre_roll.append(chunk)
            wake_word = self.kws.accept_chunk(chunk)
            if not wake_word:
                return None

            self._enter_capturing(wake_word)
            return None

        return self._process_capturing(chunk)

    def _enter_capturing(self, wake_word: str) -> None:
        self.state = DetectorState.CAPTURING
        self._wake_word = wake_word
        self._capture_chunks = list(self._pre_roll)
        self._captured_samples = 0
        self._saw_speech = False
        self.kws.reset()
        self.vad.reset()
        for buffered in self._capture_chunks:
            self.vad.accept_waveform(buffered)
            if self.vad.is_speech_detected():
                self._saw_speech = True
        self._pre_roll.clear()
        logger.info("voice.wake_triggered", wake_word=wake_word)
        if self.on_wake is not None:
            self.on_wake(wake_word)

    def _process_capturing(self, chunk: npt.NDArray[np.float32]) -> CapturedUtterance | None:
        self._capture_chunks.append(chunk)
        self._captured_samples += int(chunk.size)
        self.vad.accept_waveform(chunk)

        if self.vad.is_speech_detected():
            self._saw_speech = True

        # 说话后检测到足够长的尾部静音，Silero VAD 会自动把完整语音段压入队列。
        if not self.vad.empty():
            segment = self._drain_vad_segments()
            if segment.size > 0:
                utterance = CapturedUtterance(wake_word=self._wake_word, samples=segment)
                self.reset()
                return utterance

        if not self._saw_speech and self._captured_samples >= self._command_wait_samples:
            timed_out_wake = self._wake_word
            logger.info("voice.command_wait_timeout", wake_word=timed_out_wake)
            self.reset()
            if self.on_timeout is not None:
                self.on_timeout(timed_out_wake)
            return None

        if self._captured_samples >= self._max_record_samples:
            self.vad.flush()
            segment = self._drain_vad_segments()
            if segment.size == 0 and self._capture_chunks:
                segment = np.concatenate(self._capture_chunks)
            utterance = CapturedUtterance(wake_word=self._wake_word, samples=segment)
            self.reset()
            return utterance

        return None

    def _drain_vad_segments(self) -> npt.NDArray[np.float32]:
        parts: list[npt.NDArray[np.float32]] = []
        while not self.vad.empty():
            part = self.vad.pop_segment()
            if part.size > 0:
                parts.append(part)
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts) if len(parts) > 1 else parts[0]


__all__ = [
    "CapturedUtterance",
    "DetectorState",
    "KwsEngine",
    "SherpaKwsEngine",
    "SherpaVadEngine",
    "VadEngine",
    "WakeCommandDetector",
]
