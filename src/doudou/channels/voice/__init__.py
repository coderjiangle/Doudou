"""Local voice channel (sherpa-onnx wake/VAD + DeepSeek ASR/TTS).

保持包根轻量：不要在这里重导出会拉起 ``sherpa_onnx`` / ``sounddevice`` 的符号，
调用方一律显式导入子模块（例如 ``from doudou.channels.voice.assistant import VoiceAssistant``）。
"""

from __future__ import annotations

__all__: list[str] = []
