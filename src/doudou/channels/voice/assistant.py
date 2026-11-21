"""Always-on voice assistant loop with barge-in support during TTS playback."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt
from rich.console import Console

from doudou.agent.events import RunEvent
from doudou.channels.voice.audio import (
    INPUT_SAMPLE_RATE,
    VAD_WINDOW_SAMPLES,
    AudioInputStream,
    AudioPlayer,
    SoundDeviceMic,
    SoundDeviceSpeaker,
    float32_to_wav_bytes,
)
from doudou.channels.voice.deepseek_speech import (
    DeepSeekAsrClient,
    DeepSeekTtsClient,
    strip_wake_prefix,
)
from doudou.channels.voice.deps import ensure_voice_dependencies
from doudou.channels.voice.detector import (
    CapturedUtterance,
    KwsEngine,
    SherpaKwsEngine,
    SherpaVadEngine,
    WakeCommandDetector,
)
from doudou.channels.voice.errors import VoiceError
from doudou.channels.voice.models import ensure_voice_models
from doudou.config.loader import Settings, resolve_path
from doudou.gateway.runner import AgentRunner
from doudou.gateway.schema import RunRequest
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

_END_SENTINEL = object()


@dataclass(frozen=True, slots=True)
class ResponseOutcome:
    """单轮语音回复的执行结果（是否中途被打断、是否携带唤醒词）。"""

    interrupted: bool = False
    wake_word: str = ""


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
    """常驻本地语音交互循环（``sherpa-onnx`` 唤醒/打断 + DeepSeek ASR/TTS）。"""

    name = "voice"

    def __init__(
        self,
        settings: Settings,
        runner: AgentRunner,
        *,
        mic: AudioInputStream | None = None,
        speaker: AudioPlayer | None = None,
        detector: WakeCommandDetector | None = None,
        interrupt_kws: KwsEngine | None = None,
        asr: AsrBackend | None = None,
        tts: TtsBackend | None = None,
        key_poller: Callable[[], bool] | None = None,
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
        self._interrupt_kws = interrupt_kws
        self._key_poller = key_poller or _default_key_poller()
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
        """加载本地 ``sherpa-onnx`` 模型并初始化唤醒与打断检测器。"""
        ensure_voice_dependencies()
        if self._detector is not None and (
            not self.config.barge_in_enabled or self._interrupt_kws is not None
        ):
            return

        models_dir = resolve_path(Path.cwd(), self.config.models_dir)
        interrupt_words = list(self.config.interrupt_words)
        if self.config.interrupt_with_wake_word:
            interrupt_words.extend(self.config.wake_words)

        paths = await ensure_voice_models(
            models_dir,
            wake_words=self.config.wake_words,
            interrupt_words=interrupt_words,
            auto_download=self.config.auto_download_models,
        )
        if self._detector is None:
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
                on_wake=lambda word: self.console.print(
                    f"[bold green]已唤醒[/bold green] ({word})"
                ),
                on_timeout=lambda _word: self.console.print("[dim]未听到后续指令，回到待机[/dim]"),
            )

        if (
            self.config.barge_in_enabled
            and self._interrupt_kws is None
            and paths.kws_interrupt_keywords is not None
        ):
            self._interrupt_kws = SherpaKwsEngine(
                paths,
                keywords_file=paths.kws_interrupt_keywords,
                keywords_score=self.config.keywords_score,
                keywords_threshold=self.config.keywords_threshold,
            )

    async def run(self, *, max_turns: int | None = None) -> None:
        """启动常驻语音监听循环；``Ctrl+C`` 或达到 ``max_turns`` 时退出。"""
        await self.prepare()
        assert self._detector is not None
        self._stopped = False

        wake_label = " / ".join(self.config.wake_words)
        interrupt_hint = ""
        if self.config.barge_in_enabled and self.config.interrupt_words:
            interrupt_hint = (
                f" · 播报打断词：[bold]{' / '.join(self.config.interrupt_words)}[/bold]"
                "（或按 Space/Enter/Esc）"
            )
        self.console.print(
            f"[bold cyan]豆豆语音助手已就绪[/bold cyan] · 唤醒词：[bold]{wake_label}[/bold]"
            f"{interrupt_hint} · 音色：{self.config.voice_id}（Ctrl+C 退出）"
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

                outcome = await self._handle_utterance(utterance)
                if outcome is None:
                    await self._cooldown_and_flush()
                    continue

                turns += 1
                if max_turns is not None and turns >= max_turns:
                    break

                if outcome.interrupted and outcome.wake_word:
                    # 播报被唤醒词打断：直接进入指令采集态，免去再喊一遍唤醒词。
                    self._mic.drain()
                    self._detector.force_wake(outcome.wake_word)
                else:
                    await self._cooldown_and_flush()
        finally:
            await self._mic.stop()

    def stop(self) -> None:
        self._stopped = True

    async def _handle_utterance(self, utterance: CapturedUtterance) -> ResponseOutcome | None:
        wav_bytes = float32_to_wav_bytes(utterance.samples, sample_rate=INPUT_SAMPLE_RATE)
        try:
            asr_res = await self._asr.transcribe_wav(wav_bytes)
        except VoiceError as exc:
            self.console.print(f"[red]语音识别失败：{exc}[/red]")
            return None

        raw_text = asr_res.text.strip()
        if not raw_text:
            self.console.print("[dim]未识别到有效内容，回到待机[/dim]")
            return None

        accepted_prefixes = [*self.config.wake_words, *self.config.wake_homophones]
        matched, command = strip_wake_prefix(raw_text, accepted_prefixes)
        if self.config.strict_wake_prefix and not matched:
            logger.info("voice.rejected_non_wake_prefix", text=raw_text)
            self.console.print(f"[dim]已忽略非唤醒词语句：{raw_text}[/dim]")
            return None

        # 用户只喊了一声唤醒词（如“豆豆”）后停顿超过 0.9s，切出完整语句仍应有简短回应。
        prompt = command if command else raw_text
        self.console.print(f"\n[bold cyan]你（语音）›[/bold cyan] {raw_text}")
        return await self._respond_with_tts(prompt)

    async def _respond_with_tts(self, prompt: str) -> ResponseOutcome:
        self._mic.drain()
        if self._interrupt_kws is not None:
            self._interrupt_kws.reset()
        # 丢弃进入播报前的残留按键事件，避免误触打断。
        self._key_poller()

        await self._speaker.start()
        playback_task = asyncio.create_task(self._play_turn(prompt))

        if not self.config.barge_in_enabled:
            await playback_task
            return ResponseOutcome(interrupted=False)

        return await self._monitor_barge_in(playback_task)

    async def _play_turn(self, prompt: str) -> None:
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
        except asyncio.CancelledError:
            tts_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await tts_task
            raise
        finally:
            with suppress(asyncio.CancelledError):
                await text_queue.put(_END_SENTINEL)

        self.console.print()
        if final_error:
            self.console.print(f"[red]执行出错：{final_error}[/red]")

        try:
            await tts_task
            await self._speaker.finish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("voice.tts_playback_failed", error=str(exc))
            self.console.print(f"[yellow]语音播报失败（文字回复已展示）：{exc}[/yellow]")
            await self._speaker.abort()

    async def _monitor_barge_in(self, playback_task: asyncio.Task[None]) -> ResponseOutcome:
        mic_eof = False
        mic_task: asyncio.Task[npt.NDArray[np.float32] | None] | None = None
        try:
            while not playback_task.done():
                if self._key_poller():
                    return await self._abort_playback(playback_task, mic_task, reason="按键打断")

                if self._interrupt_kws is not None and not mic_eof and mic_task is None:
                    mic_task = asyncio.create_task(self._mic.read_chunk())

                wait_targets: set[asyncio.Task[Any]] = {playback_task}
                if mic_task is not None:
                    wait_targets.add(mic_task)

                done, _ = await asyncio.wait(
                    wait_targets,
                    timeout=0.06,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if mic_task is not None and mic_task in done:
                    chunk = mic_task.result()
                    mic_task = None
                    if chunk is None:
                        mic_eof = True
                    elif self._interrupt_kws is not None:
                        hit = self._interrupt_kws.accept_chunk(chunk)
                        if hit:
                            wake = (
                                hit
                                if (
                                    self.config.interrupt_with_wake_word
                                    and hit in self.config.wake_words
                                )
                                else ""
                            )
                            return await self._abort_playback(
                                playback_task,
                                None,
                                reason=hit,
                                wake_word=wake,
                            )

                if playback_task in done:
                    break

            await playback_task
            return ResponseOutcome(interrupted=False)
        finally:
            if mic_task is not None and not mic_task.done():
                mic_task.cancel()
                with suppress(asyncio.CancelledError):
                    await mic_task

    async def _abort_playback(
        self,
        playback_task: asyncio.Task[None],
        mic_task: asyncio.Task[npt.NDArray[np.float32] | None] | None,
        *,
        reason: str,
        wake_word: str = "",
    ) -> ResponseOutcome:
        logger.info("voice.barge_in", reason=reason, wake_word=wake_word or None)
        await self._speaker.abort()
        if mic_task is not None and not mic_task.done():
            mic_task.cancel()
            with suppress(asyncio.CancelledError):
                await mic_task
        if not playback_task.done():
            playback_task.cancel()
            with suppress(asyncio.CancelledError):
                await playback_task
        self.console.print(f"\n[bold yellow][已打断播报：{reason}][/bold yellow]")
        return ResponseOutcome(interrupted=True, wake_word=wake_word)

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
        if self._interrupt_kws is not None:
            self._interrupt_kws.reset()
        self._detector.reset()


def _default_key_poller() -> Callable[[], bool]:
    """返回非阻塞终端按键轮询器（Space / Enter / Esc 触发打断）；非 TTY 环境返回 False。"""
    if not sys.stdin.isatty():
        return lambda: False
    if sys.platform == "win32":
        import msvcrt

        def _poll_win() -> bool:
            hit = False
            while msvcrt.kbhit():
                if msvcrt.getwch() in {" ", "\r", "\n", "\x1b"}:
                    hit = True
            return hit

        return _poll_win
    return lambda: False


__all__ = ["ResponseOutcome", "VoiceAssistant"]
