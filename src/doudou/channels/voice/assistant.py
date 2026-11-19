"""Always-on voice assistant loop connecting mic, detector, AgentRunner and speaker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Protocol

from rich.console import Console

from doudou.agent.events import RunEvent
from doudou.channels.voice.audio import (
    INPUT_SAMPLE_RATE,
    VAD_WINDOW_SAMPLES,
    AudioInputStream,
    AudioPlayer,
    SoundDeviceMic,
    SoundDeviceSpeaker,
    VoiceError,
    float32_to_wav_bytes,
)
from doudou.channels.voice.deepseek_speech import (
    DeepSeekAsrClient,
    DeepSeekTtsClient,
    strip_wake_prefix,
)
from doudou.channels.voice.detector import (
    CapturedUtterance,
    SherpaKwsEngine,
    SherpaVadEngine,
    WakeCommandDetector,
)
from doudou.channels.voice.models import ensure_voice_models
from doudou.config.loader import Settings, resolve_path
from doudou.gateway.runner import AgentRunner
from doudou.gateway.schema import RunRequest
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

_END_SENTINEL = object()


class AnyResult(Protocol):
    @property
    def text(self) -> str: ...


class AsrBackend(Protocol):
    async def transcribe_wav(self, wav_bytes: bytes) -> AnyResult: ...


class TtsBackend(Protocol):
    async def stream_synthesize(
        self,
        text_chunks: AsyncIterator[str],
        on_audio: Callable[[bytes], Awaitable[None]],
    ) -> None: ...


class VoiceAssistant:
    """常驻本地语音交互循环（``sherpa-onnx`` 唤醒断句 + DeepSeek ASR/TTS）。"""

    name = "voice"

    def __init__(
        self,
        settings: Settings,
        runner: AgentRunner,
        *,
        mic: AudioInputStream | None = None,
        speaker: AudioPlayer | None = None,
        detector: WakeCommandDetector | None = None,
        asr: AsrBackend | None = None,
        tts: TtsBackend | None = None,
        console: Console | None = None,
        conversation_id: str = "voice-local",
    ) -> None:
        self.settings = settings
        self.config = settings.app.channels.voice
        self.runner = runner
        self.console = console or Console()
        self.conversation_id = conversation_id

        api_key = settings.secrets.voice_api_key or ""
        if (asr is None or tts is None) and not api_key:
            raise VoiceError(
                "缺少 DeepSeek 语音 API Key。请在 .env 中配置 DOUDOU_CHANNELS_VOICE_API_KEY"
                "（或 DOUDOU_TOOLS_WEB_SEARCH_API_KEY）。"
            )

        self._mic = mic or SoundDeviceMic(
            sample_rate=INPUT_SAMPLE_RATE,
            chunk_samples=VAD_WINDOW_SAMPLES,
            device=self.config.input_device,
        )
        self._speaker = speaker or SoundDeviceSpeaker(device=self.config.output_device)
        self._detector = detector
        self._asr: AsrBackend = asr or DeepSeekAsrClient(
            api_key=api_key, endpoint=self.config.asr_url
        )
        self._tts: TtsBackend = tts or DeepSeekTtsClient(
            api_key=api_key,
            ws_url=self.config.tts_ws_url,
            http_url=self.config.tts_url,
            voice_id=self.config.voice_id,
            filter_markdown=self.config.filter_markdown,
        )
        self._stopped = False

    async def prepare(self) -> None:
        """加载本地 ``sherpa-onnx`` 模型并初始化检测器。"""
        if self._detector is not None:
            return
        models_dir = resolve_path(Path.cwd(), self.config.models_dir)
        paths = await ensure_voice_models(
            models_dir,
            wake_words=self.config.wake_words,
            auto_download=self.config.auto_download_models,
        )
        kws = SherpaKwsEngine(
            paths,
            keywords_score=self.config.keywords_score,
            keywords_threshold=self.config.keywords_threshold,
        )
        vad = SherpaVadEngine(
            paths,
            threshold=self.config.vad_threshold,
            min_silence_duration=self.config.silence_duration_seconds,
            max_speech_duration=self.config.max_record_seconds,
        )
        self._detector = WakeCommandDetector(
            kws,
            vad,
            command_wait_seconds=self.config.command_wait_seconds,
            max_record_seconds=self.config.max_record_seconds,
            on_wake=lambda word: self.console.print(f"[bold green]已唤醒[/bold green] ({word})"),
            on_timeout=lambda _word: self.console.print("[dim]未听到后续指令，回到待机[/dim]"),
        )

    async def run(self, *, max_turns: int | None = None) -> None:
        """启动常驻语音监听循环；``Ctrl+C`` 或达到 ``max_turns`` 时退出。"""
        await self.prepare()
        assert self._detector is not None
        self._stopped = False

        wake_label = " / ".join(self.config.wake_words)
        self.console.print(
            f"[bold cyan]豆豆语音助手已就绪[/bold cyan] · 唤醒词：[bold]{wake_label}[/bold] · "
            f"音色：{self.config.voice_id}（Ctrl+C 退出）"
        )
        self.console.print(f"[dim]待机监听中，请说“{self.config.wake_words[0]}，...”[/dim]")

        await self._mic.start()
        turns = 0
        try:
            while not self._stopped:
                chunk = await self._mic.read_chunk()
                if chunk is None:
                    break
                utterance = self._detector.feed(chunk)
                if utterance is None:
                    continue

                # 识别与回复阶段暂停消费唤醒（播报完再清掉残留音频），防止音箱声音回流触发。
                handled = await self._handle_utterance(utterance)
                await self._cooldown_and_flush()
                if handled:
                    turns += 1
                    if max_turns is not None and turns >= max_turns:
                        break
        finally:
            await self._mic.stop()

    def stop(self) -> None:
        self._stopped = True

    async def _handle_utterance(self, utterance: CapturedUtterance) -> bool:
        wav_bytes = float32_to_wav_bytes(utterance.samples, sample_rate=INPUT_SAMPLE_RATE)
        try:
            asr_res = await self._asr.transcribe_wav(wav_bytes)
        except VoiceError as exc:
            self.console.print(f"[red]语音识别失败：{exc}[/red]")
            return False

        raw_text = asr_res.text.strip()
        if not raw_text:
            self.console.print("[dim]未识别到有效内容，回到待机[/dim]")
            return False

        accepted_prefixes = [*self.config.wake_words, *self.config.wake_homophones]
        matched, command = strip_wake_prefix(raw_text, accepted_prefixes)
        if self.config.strict_wake_prefix and not matched:
            logger.info("voice.rejected_non_wake_prefix", text=raw_text)
            self.console.print(f"[dim]已忽略非唤醒词语句：{raw_text}[/dim]")
            return False

        # 用户只喊了一声唤醒词（如“豆豆”）后停顿超过 0.9s，切出完整语句仍应有简短回应。
        prompt = command if command else raw_text
        self.console.print(f"\n[bold cyan]你（语音）›[/bold cyan] {raw_text}")
        await self._respond_with_tts(prompt)
        return True

    async def _respond_with_tts(self, prompt: str) -> None:
        request = RunRequest(
            text=prompt,
            conversation_id=self.conversation_id,
            channel=self.name,
        )
        thread_id = await self.runner.resolve_thread(request)

        text_queue: asyncio.Queue[object] = asyncio.Queue()

        async def _text_iter() -> AsyncIterator[str]:
            while True:
                item = await text_queue.get()
                if item is _END_SENTINEL:
                    break
                if isinstance(item, str) and item:
                    yield item

        await self._speaker.start()
        tts_task = asyncio.create_task(
            self._tts.stream_synthesize(_text_iter(), self._speaker.write_pcm)
        )

        self.console.print("[bold magenta]豆豆 ›[/bold magenta] ", end="")
        final_error: str | None = None
        try:
            async for event in self.runner.astream(request, thread_id=thread_id):
                await self._consume_event(event, text_queue)
                if event.type == "error":
                    final_error = event.text
        finally:
            await text_queue.put(_END_SENTINEL)

        self.console.print()
        if final_error:
            self.console.print(f"[red]执行出错：{final_error}[/red]")

        try:
            await tts_task
            await self._speaker.finish()
        except Exception as exc:
            logger.warning("voice.tts_playback_failed", error=str(exc))
            self.console.print(f"[yellow]语音播报失败（文字回复已展示）：{exc}[/yellow]")
            await self._speaker.abort()

    async def _consume_event(self, event: RunEvent, text_queue: asyncio.Queue[object]) -> None:
        if event.type == "text_delta" and event.text:
            self.console.print(event.text, end="", markup=False, highlight=False)
            await text_queue.put(event.text)
        elif event.type == "tool_call":
            self.console.print(f"\n[dim]  ↳ 调用工具：{event.tool_name}[/dim]")
        elif event.type == "error" and event.text:
            await text_queue.put(f"出错了，{event.text}")

    async def _cooldown_and_flush(self) -> None:
        assert self._detector is not None
        self._mic.drain()
        if self.config.cooldown_seconds > 0:
            await asyncio.sleep(self.config.cooldown_seconds)
            self._mic.drain()
        self._detector.reset()


__all__ = ["VoiceAssistant"]
