"""Optional dependency checks for the local voice channel."""

from __future__ import annotations

from collections.abc import Sequence

from doudou.channels.voice.errors import VoiceDependencyError
from doudou.utils.optional import MissingDependencyError, require_modules

#: 完整语音助手：推理、采集与唤醒词编码。
VOICE_MODULES = ("numpy", "sherpa_onnx", "sounddevice", "pypinyin")
#: ``voice download-models`` 只需要生成唤醒词表。
VOICE_MODEL_MODULES = ("pypinyin",)
#: ``voice devices`` 只枚举音频设备。
VOICE_DEVICE_MODULES = ("sounddevice",)


def ensure_voice_dependencies(modules: Sequence[str] = VOICE_MODULES) -> None:
    """检查语音依赖；缺失时抛出带安装命令的 :class:`VoiceDependencyError`。"""
    try:
        require_modules(feature="语音功能", modules=modules, extra="voice")
    except MissingDependencyError as exc:
        raise VoiceDependencyError(str(exc)) from exc


__all__ = [
    "VOICE_DEVICE_MODULES",
    "VOICE_MODEL_MODULES",
    "VOICE_MODULES",
    "ensure_voice_dependencies",
]
