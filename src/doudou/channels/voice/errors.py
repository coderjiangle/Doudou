"""Voice-channel exceptions.

Kept in a dependency-light module so CLI entry points can import them even when
the optional voice dependencies (numpy/sherpa-onnx/sounddevice) are not installed.
"""

from __future__ import annotations


class VoiceError(RuntimeError):
    """语音采集、唤醒模型或 ASR/TTS 调用失败。"""


class VoiceDependencyError(VoiceError):
    """语音可选依赖未安装。"""


__all__ = ["VoiceDependencyError", "VoiceError"]
