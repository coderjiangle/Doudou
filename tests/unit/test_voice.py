"""本地语音通道：唤醒词、VAD 状态机、模型下载、DeepSeek ASR/TTS 与助手闭环测试（全离线）。"""

from __future__ import annotations

import io
import json
import tarfile
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import numpy.typing as npt
import pytest
from langchain_core.messages import AIMessage, BaseMessage
from rich.console import Console
from typer.testing import CliRunner

from doudou.agent.factory import build_agent
from doudou.agent.testing import ScriptedChatModel
from doudou.channels.voice.assistant import VoiceAssistant
from doudou.channels.voice.audio import (
    INPUT_SAMPLE_RATE,
    VAD_WINDOW_SAMPLES,
    VoiceError,
    float32_to_wav_bytes,
    pcm16_bytes_to_float32,
)
from doudou.channels.voice.deepseek_speech import (
    MAX_ASR_BYTES,
    AsrResult,
    DeepSeekAsrClient,
    DeepSeekTtsClient,
    strip_wake_prefix,
)
from doudou.channels.voice.detector import DetectorState, WakeCommandDetector
from doudou.channels.voice.models import (
    KWS_ARCHIVE_URL,
    KWS_MODEL_DIRNAME,
    SILERO_VAD_URL,
    encode_wake_word,
    ensure_voice_models,
    write_keywords_file,
)
from doudou.cli.main import app
from doudou.config.loader import Settings
from doudou.gateway.runner import AgentRunner

# --------------------------------------------------------------------------- 音频与前缀


def test_float32_wav_roundtrip() -> None:
    samples = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
    wav_bytes = float32_to_wav_bytes(samples, sample_rate=INPUT_SAMPLE_RATE)

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == INPUT_SAMPLE_RATE
        frames = wf.readframes(wf.getnframes())

    decoded = pcm16_bytes_to_float32(frames)
    assert decoded.shape == samples.shape
    assert np.allclose(decoded, samples, atol=1e-3)


@pytest.mark.parametrize(
    ("raw", "expected_match", "expected_cmd"),
    [
        ("豆豆，打开哔哩哔哩", True, "打开哔哩哔哩"),
        ("  「豆豆」帮我暂停一下！", True, "帮我暂停一下！"),
        ("逗逗继续播放", True, "继续播放"),
        ("豆豆", True, ""),
        ("今天英雄联盟总决赛太精彩了", False, "今天英雄联盟总决赛太精彩了"),
        ("   ", False, ""),
    ],
)
def test_strip_wake_prefix(raw: str, expected_match: bool, expected_cmd: str) -> None:
    matched, cmd = strip_wake_prefix(raw, ["豆豆", "逗逗"])
    assert matched is expected_match
    assert cmd == expected_cmd


# --------------------------------------------------------------------------- 唤醒词与模型准备


def test_encode_wake_word_generates_primary_and_neutral_variant() -> None:
    valid = {"d", "òu", "ou", "l", "ǎo", "à"}
    lines = encode_wake_word("豆豆", valid_tokens=valid)
    assert lines == ["d òu d òu @豆豆", "d òu d ou @豆豆"]


def test_write_keywords_file_and_rejects_empty(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens.txt"
    tokens.write_text("d 1\nòu 2\nou 3\n", encoding="utf-8")
    out = tmp_path / "kw.txt"

    write_keywords_file(out, ["豆豆"], tokens_path=tokens)
    content = out.read_text(encoding="utf-8")
    assert "d òu d òu @豆豆" in content
    assert "d òu d ou @豆豆" in content

    with pytest.raises(VoiceError, match="不能为空"):
        write_keywords_file(out, [""], tokens_path=tokens)


async def test_ensure_voice_models_downloads_and_extracts_offline(tmp_path: Path) -> None:
    archive_buf = io.BytesIO()
    with tarfile.open(fileobj=archive_buf, mode="w:bz2") as tf:
        for rel, body in (
            ("tokens.txt", b"d 1\n\xc3\xb2u 2\nou 3\n"),
            ("encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx", b"enc"),
            ("decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx", b"dec"),
            ("joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx", b"joi"),
        ):
            info = tarfile.TarInfo(name=f"{KWS_MODEL_DIRNAME}/{rel}")
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    archive_bytes = archive_buf.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SILERO_VAD_URL:
            return httpx.Response(200, content=b"vad-onnx")
        if str(request.url) == KWS_ARCHIVE_URL:
            return httpx.Response(200, content=archive_bytes)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        paths = await ensure_voice_models(
            tmp_path / "voice",
            wake_words=["豆豆"],
            auto_download=True,
            client=client,
        )

    assert paths.vad_model.read_bytes() == b"vad-onnx"
    assert paths.kws_encoder.read_bytes() == b"enc"
    assert "d òu d òu @豆豆" in paths.kws_keywords.read_text(encoding="utf-8")


async def test_ensure_voice_models_fails_when_auto_download_disabled(tmp_path: Path) -> None:
    with pytest.raises(VoiceError, match="auto_download_models=false"):
        await ensure_voice_models(tmp_path / "empty", wake_words=["豆豆"], auto_download=False)


# --------------------------------------------------------------------------- VAD + KWS 状态机


class _FakeKws:
    def __init__(self, trigger_at_call: int | None = None) -> None:
        self.trigger_at_call = trigger_at_call
        self.calls = 0
        self.resets = 0

    def accept_chunk(self, chunk: npt.NDArray[np.float32]) -> str:
        self.calls += 1
        if self.trigger_at_call is not None and self.calls == self.trigger_at_call:
            return "豆豆"
        return ""

    def reset(self) -> None:
        self.resets += 1


class _FakeVad:
    def __init__(self, *, emit_after_chunks: int | None = None, speech: bool = True) -> None:
        self.emit_after_chunks = emit_after_chunks
        self.speech = speech
        self.chunks_seen = 0
        self.segments: list[npt.NDArray[np.float32]] = []
        self.flushed = False
        self.resets = 0

    def accept_waveform(self, chunk: npt.NDArray[np.float32]) -> None:
        self.chunks_seen += 1
        if self.emit_after_chunks is not None and self.chunks_seen >= self.emit_after_chunks:
            self.segments.append(np.ones(1600, dtype=np.float32) * 0.2)
            self.emit_after_chunks = None

    def is_speech_detected(self) -> bool:
        return self.speech

    def empty(self) -> bool:
        return not self.segments

    def pop_segment(self) -> npt.NDArray[np.float32]:
        return self.segments.pop(0)

    def flush(self) -> None:
        self.flushed = True
        if self.speech and not self.segments:
            self.segments.append(np.ones(1024, dtype=np.float32) * 0.1)

    def reset(self) -> None:
        self.resets += 1
        self.chunks_seen = 0
        self.segments.clear()


def test_detector_captures_utterance_after_wake() -> None:
    wakes: list[str] = []
    kws = _FakeKws(trigger_at_call=3)
    vad = _FakeVad(emit_after_chunks=6, speech=True)
    detector = WakeCommandDetector(kws, vad, on_wake=wakes.append)
    chunk = np.zeros(VAD_WINDOW_SAMPLES, dtype=np.float32)

    assert detector.feed(chunk) is None
    assert detector.feed(chunk) is None
    assert detector.feed(chunk) is None
    assert detector.state is DetectorState.CAPTURING
    assert wakes == ["豆豆"]

    # pre_roll 有 3 帧 + 后续 3 帧 → 共 6 帧触发 VAD 输出完整语音段
    assert detector.feed(chunk) is None
    assert detector.feed(chunk) is None
    utterance = detector.feed(chunk)
    assert utterance is not None
    assert utterance.wake_word == "豆豆"
    assert utterance.samples.size == 1600
    assert detector.state is DetectorState.IDLE


def test_detector_returns_to_idle_on_command_wait_timeout() -> None:
    timeouts: list[str] = []
    kws = _FakeKws(trigger_at_call=1)
    vad = _FakeVad(emit_after_chunks=None, speech=False)
    detector = WakeCommandDetector(
        kws,
        vad,
        command_wait_seconds=0.1,
        on_timeout=timeouts.append,
    )
    chunk = np.zeros(VAD_WINDOW_SAMPLES, dtype=np.float32)

    assert detector.feed(chunk) is None  # 唤醒
    for _ in range(5):
        assert detector.feed(chunk) is None
    assert detector.state is DetectorState.IDLE
    assert timeouts == ["豆豆"]


def test_detector_flushes_at_max_record_seconds() -> None:
    kws = _FakeKws(trigger_at_call=1)
    vad = _FakeVad(emit_after_chunks=None, speech=True)
    detector = WakeCommandDetector(kws, vad, max_record_seconds=0.1)
    chunk = np.zeros(VAD_WINDOW_SAMPLES, dtype=np.float32)

    assert detector.feed(chunk) is None  # 唤醒
    utterance = None
    for _ in range(5):
        utterance = detector.feed(chunk)
        if utterance is not None:
            break
    assert utterance is not None
    assert vad.flushed is True
    assert detector.state is DetectorState.IDLE


# --------------------------------------------------------------------------- DeepSeek ASR / TTS


async def test_deepseek_asr_transcribes_wav_with_mock_transport() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        assert request.url.path == "/v1/tools/asr"
        assert b"speech.wav" in request.content
        return httpx.Response(
            200,
            json={"request_id": "req-1", "text": "豆豆，现在几点了", "duration_ms": 1500},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        asr = DeepSeekAsrClient(api_key="sk-voice-test", client=client)
        res = await asr.transcribe_wav(b"RIFF....WAVE")

    assert seen_headers.get("authorization") == "Bearer sk-voice-test"
    assert res.text == "豆豆，现在几点了"
    assert res.duration_ms == 1500


async def test_deepseek_asr_rejects_oversized_or_empty_audio() -> None:
    asr = DeepSeekAsrClient(api_key="sk-test")
    with pytest.raises(VoiceError, match="为空"):
        await asr.transcribe_wav(b"")
    with pytest.raises(VoiceError, match="8 MiB"):
        await asr.transcribe_wav(b"x" * (MAX_ASR_BYTES + 1))


class _FakeWs:
    def __init__(self, responses: list[bytes | str]) -> None:
        self.sent: list[dict[str, Any]] = []
        self._responses = list(responses)
        self._idx = 0

    async def __aenter__(self) -> _FakeWs:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def __aiter__(self) -> _FakeWs:
        return self

    async def __anext__(self) -> bytes | str:
        if self._idx >= len(self._responses):
            raise StopAsyncIteration
        item = self._responses[self._idx]
        self._idx += 1
        return item


async def test_deepseek_tts_websocket_streams_pcm() -> None:
    fake_ws = _FakeWs(
        [b"\x01\x02", b"\x03\x04", json.dumps({"event": "finish", "code": "success"})]
    )
    captured_url = ""
    captured_headers: dict[str, str] = {}

    def ws_factory(url: str, *, additional_headers: dict[str, str]) -> _FakeWs:
        nonlocal captured_url, captured_headers
        captured_url = url
        captured_headers = additional_headers
        return fake_ws

    tts = DeepSeekTtsClient(api_key="sk-tts", voice_id="tide", ws_connect=ws_factory)
    received: list[bytes] = []

    async def chunks() -> AsyncIterator[str]:
        yield "汪！"
        yield "帮你打开好啦。"

    async def on_audio(data: bytes) -> None:
        received.append(data)

    await tts.stream_synthesize(chunks(), on_audio)

    assert "voice_id=tide" in captured_url
    assert "format=pcm" in captured_url
    assert captured_headers["Authorization"] == "Bearer sk-tts"
    assert fake_ws.sent == [
        {"event": "text", "text": "汪！"},
        {"event": "text", "text": "帮你打开好啦。"},
        {"event": "finish_text"},
    ]
    assert received == [b"\x01\x02", b"\x03\x04"]


async def test_deepseek_tts_http_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["text"] == "你好"
        assert body["format"] == "pcm"
        return httpx.Response(200, content=b"PCM_DATA")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tts = DeepSeekTtsClient(api_key="sk-tts", http_client=client)
        assert await tts.synthesize_pcm("你好") == b"PCM_DATA"


# --------------------------------------------------------------------------- VoiceAssistant 端到端闭环


class _FakeMic:
    def __init__(self, chunks: list[npt.NDArray[np.float32]]) -> None:
        self._chunks = list(chunks)
        self.drained = 0
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def read_chunk(self) -> npt.NDArray[np.float32] | None:
        if not self._chunks:
            return None
        return self._chunks.pop(0)

    def drain(self) -> int:
        self.drained += 1
        return 0

    async def stop(self) -> None:
        self.stopped = True


class _FakeSpeaker:
    def __init__(self) -> None:
        self.pcm_chunks: list[bytes] = []
        self.finished = False
        self.aborted = False

    async def start(self) -> None:
        return None

    async def write_pcm(self, pcm_bytes: bytes) -> None:
        self.pcm_chunks.append(pcm_bytes)

    async def finish(self) -> None:
        self.finished = True

    async def abort(self) -> None:
        self.aborted = True


class _FakeAsr:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls = 0

    async def transcribe_wav(self, wav_bytes: bytes) -> AsrResult:
        self.calls += 1
        assert wav_bytes.startswith(b"RIFF")
        return AsrResult(text=self.texts.pop(0), duration_ms=500)


class _FakeTts:
    def __init__(self) -> None:
        self.spoken_text = ""

    async def stream_synthesize(
        self,
        text_chunks: AsyncIterator[str],
        on_audio: Callable[[bytes], Awaitable[None]],
    ) -> None:
        parts: list[str] = []
        async for chunk in text_chunks:
            parts.append(chunk)
        self.spoken_text = "".join(parts)
        await on_audio(b"\x10\x20\x30\x40")


async def test_voice_assistant_filters_non_wake_and_speaks_response(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        config_yaml=(
            "channels:\n"
            "  voice:\n"
            "    enabled: true\n"
            "    cooldown_seconds: 0.0\n"
            "    strict_wake_prefix: true\n"
        )
    )
    recorded_messages: list[list[BaseMessage]] = []

    def _reply(messages: list[BaseMessage]) -> AIMessage:
        recorded_messages.append(list(messages))
        return AIMessage(content="汪！现在给你播报。")

    model = ScriptedChatModel(steps=[_reply])
    bundle = build_agent(settings, model=model)
    runner = AgentRunner(bundle)

    # 两次唤醒输出：第一次被 ASR 二次过滤丢弃（非“豆豆”开头），第二次正常执行并播报。
    kws = _FakeKws(trigger_at_call=1)
    vad = _FakeVad(emit_after_chunks=2, speech=True)

    class _TwoShotDetector(WakeCommandDetector):
        def __init__(self) -> None:
            super().__init__(kws, vad)
            self._emitted = 0

        def feed(self, chunk: npt.NDArray[np.float32]) -> Any:
            from doudou.channels.voice.detector import CapturedUtterance

            self._emitted += 1
            if self._emitted in (1, 2):
                return CapturedUtterance(
                    wake_word="豆豆",
                    samples=np.ones(800, dtype=np.float32) * 0.1,
                )
            return None

    chunk = np.zeros(VAD_WINDOW_SAMPLES, dtype=np.float32)
    mic = _FakeMic([chunk, chunk, chunk])
    speaker = _FakeSpeaker()
    asr = _FakeAsr(["解说员正在分析团战", "豆豆，你好呀"])
    tts = _FakeTts()

    assistant = VoiceAssistant(
        settings,
        runner,
        mic=mic,
        speaker=speaker,
        detector=_TwoShotDetector(),
        asr=asr,
        tts=tts,
        console=Console(file=io.StringIO()),
    )
    await assistant.run(max_turns=1)

    assert asr.calls == 2
    assert len(recorded_messages) == 1
    assert "你好呀" in str(recorded_messages[0][-1].content)
    assert tts.spoken_text == "汪！现在给你播报。"
    assert speaker.pcm_chunks == [b"\x10\x20\x30\x40"]
    assert speaker.finished is True
    assert mic.drained >= 2
    assert mic.stopped is True


async def test_voice_assistant_executes_post_wake_command_by_default(
    settings_factory: Callable[..., Settings],
) -> None:
    """默认 strict_wake_prefix=false：唤醒词已在本地触发，后续指令即使不含前缀也能执行。"""
    settings = settings_factory(
        config_yaml=("channels:\n  voice:\n    enabled: true\n    cooldown_seconds: 0.0\n")
    )
    recorded_messages: list[list[BaseMessage]] = []

    def _reply(messages: list[BaseMessage]) -> AIMessage:
        recorded_messages.append(list(messages))
        return AIMessage(content="我在趴着晒太阳呢。")

    model = ScriptedChatModel(steps=[_reply])
    runner = AgentRunner(build_agent(settings, model=model))

    kws = _FakeKws(trigger_at_call=1)
    vad = _FakeVad(emit_after_chunks=2, speech=True)

    class _OneShotDetector(WakeCommandDetector):
        def __init__(self) -> None:
            super().__init__(kws, vad)
            self._done = False

        def feed(self, chunk: npt.NDArray[np.float32]) -> Any:
            from doudou.channels.voice.detector import CapturedUtterance

            if not self._done:
                self._done = True
                return CapturedUtterance(
                    wake_word="豆豆",
                    samples=np.ones(800, dtype=np.float32) * 0.1,
                )
            return None

    chunk = np.zeros(VAD_WINDOW_SAMPLES, dtype=np.float32)
    mic = _FakeMic([chunk, chunk])
    speaker = _FakeSpeaker()
    asr = _FakeAsr(["你在干什么？"])
    tts = _FakeTts()

    assistant = VoiceAssistant(
        settings,
        runner,
        mic=mic,
        speaker=speaker,
        detector=_OneShotDetector(),
        asr=asr,
        tts=tts,
        console=Console(file=io.StringIO()),
    )
    await assistant.run(max_turns=1)

    assert len(recorded_messages) == 1
    assert "你在干什么？" in str(recorded_messages[0][-1].content)
    assert tts.spoken_text == "我在趴着晒太阳呢。"


def test_secrets_voice_api_key_falls_back_to_deepseek(
    settings_factory: Callable[..., Settings],
) -> None:
    s1 = settings_factory(
        config_yaml="tools:\n  web_search:\n    provider: deepseek\n",
        env={"DOUDOU_TOOLS_WEB_SEARCH_API_KEY": "sk-ds-search"},
    )
    assert s1.secrets.voice_api_key == "sk-ds-search"

    s2 = settings_factory(
        config_yaml="tools:\n  web_search:\n    provider: deepseek\n",
        env={
            "DOUDOU_TOOLS_WEB_SEARCH_API_KEY": "sk-ds-search",
            "DOUDOU_CHANNELS_VOICE_API_KEY": "sk-voice-only",
        },
    )
    assert s2.secrets.voice_api_key == "sk-voice-only"


def test_cli_voice_help_and_devices_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    help_res = runner.invoke(app, ["voice", "--help"])
    assert help_res.exit_code == 0
    assert "download-models" in help_res.output
    assert "devices" in help_res.output

    import sounddevice as sd

    monkeypatch.setattr(
        sd,
        "query_devices",
        lambda: [{"name": "Test Mic", "max_input_channels": 1, "max_output_channels": 2}],
    )
    monkeypatch.setattr(sd.default, "device", [0, 0])
    dev_res = runner.invoke(app, ["voice", "devices"])
    assert dev_res.exit_code == 0
    assert "Test Mic" in dev_res.output
