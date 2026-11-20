"""sherpa-onnx local VAD / KWS model management and pinyin keyword generation."""

from __future__ import annotations

import asyncio
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from doudou.channels.voice.audio import VoiceError
from doudou.observability.logging import get_logger
from doudou.utils.async_utils import retry_async

logger = get_logger(__name__)

SILERO_VAD_FILENAME = "silero_vad.onnx"
SILERO_VAD_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
)
KWS_MODEL_DIRNAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
KWS_ARCHIVE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"
)
KEYWORDS_FILENAME = "doudou_keywords.txt"
INTERRUPT_KEYWORDS_FILENAME = "doudou_interrupt_keywords.txt"

_ENCODER_NAME = "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_DECODER_NAME = "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
_JOINER_NAME = "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"

#: 常见声调到轻声（无声调）的映射，用于为叠词或口语唤醒词自动补一条轻声尾音变体。
_TONE_STRIP = str.maketrans("āáǎàōóǒòēéěèīíǐìūúǔùǖǘǚǜü", "aaaaooooeeeeiiiiuuuuüüüüü")


@dataclass(frozen=True, slots=True)
class VoiceModelPaths:
    """本地 ``sherpa-onnx`` 推理所需的全部文件路径。"""

    vad_model: Path
    kws_tokens: Path
    kws_encoder: Path
    kws_decoder: Path
    kws_joiner: Path
    kws_keywords: Path
    kws_interrupt_keywords: Path | None = None


def load_token_set(tokens_path: Path) -> set[str]:
    """读取 ``tokens.txt`` 中的全部有效建模单元（声母 / 韵母 / 字母）。"""
    tokens: set[str] = set()
    for raw in tokens_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        tokens.add(line.split()[0])
    return tokens


def encode_wake_word(word: str, *, valid_tokens: set[str]) -> list[str]:
    """把中文唤醒词转成 ``sherpa-onnx`` KWS 所需的拼音声韵母行。

    会自动为末尾音节补充轻声变体（例如 ``豆豆`` 同时生成 ``d òu d òu @豆豆``
    与 ``d òu d ou @豆豆``），提升日常口语唤醒率。
    """
    from pypinyin import Style, pinyin
    from pypinyin.contrib.tone_convert import to_finals_tone, to_initials

    cleaned = word.strip()
    if not cleaned:
        return []

    units: list[str] = []
    for item in pinyin(cleaned, style=Style.TONE):
        syllable = item[0]
        initial = to_initials(syllable, strict=False)
        final = to_finals_tone(syllable, strict=False)
        if initial:
            units.append(initial)
        if final:
            units.append(final)

    if not units:
        raise VoiceError(f"无法将唤醒词 {word!r} 转换为拼音音素。")

    missing = [unit for unit in units if unit not in valid_tokens]
    if missing:
        raise VoiceError(f"唤醒词 {word!r} 包含模型不支持的音素：{', '.join(missing)}。")

    lines = [f"{' '.join(units)} @{cleaned}"]
    neutral_last = units[-1].translate(_TONE_STRIP)
    if neutral_last != units[-1] and neutral_last in valid_tokens:
        variant = [*units[:-1], neutral_last]
        lines.append(f"{' '.join(variant)} @{cleaned}")
    return lines


def write_keywords_file(
    path: Path,
    wake_words: Sequence[str],
    *,
    tokens_path: Path,
) -> Path:
    """根据配置的 ``wake_words`` 生成 KWS ``keywords.txt``。"""
    valid_tokens = load_token_set(tokens_path)
    lines: list[str] = []
    seen: set[str] = set()
    for word in wake_words:
        for line in encode_wake_word(word, valid_tokens=valid_tokens):
            if line not in seen:
                seen.add(line)
                lines.append(line)
    if not lines:
        raise VoiceError("channels.voice.wake_words 不能为空。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def ensure_voice_models(
    models_dir: Path,
    *,
    wake_words: Sequence[str],
    interrupt_words: Sequence[str] = (),
    auto_download: bool = True,
    client: httpx.AsyncClient | None = None,
) -> VoiceModelPaths:
    """检查本地 VAD/KWS 模型是否存在；缺失时按需自动下载并生成唤醒词表。"""
    root = await asyncio.to_thread(models_dir.resolve)
    vad_path = root / SILERO_VAD_FILENAME
    kws_dir = root / KWS_MODEL_DIRNAME
    tokens_path = kws_dir / "tokens.txt"
    encoder_path = kws_dir / _ENCODER_NAME
    decoder_path = kws_dir / _DECODER_NAME
    joiner_path = kws_dir / _JOINER_NAME

    missing_vad, missing_kws = await asyncio.to_thread(
        _check_missing_files, vad_path, (tokens_path, encoder_path, decoder_path, joiner_path)
    )
    if (missing_vad or missing_kws) and not auto_download:
        raise VoiceError(
            f"本地语音模型不完整（目录：{root}），且 auto_download_models=false。"
            "请开启 auto_download_models 或手动运行 doudou voice download-models。"
        )

    if missing_vad or missing_kws:
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        if missing_vad:
            logger.info("voice.model_downloading", model=SILERO_VAD_FILENAME, dest=str(vad_path))
            data = await _download_bytes(SILERO_VAD_URL, client=client, label="silero_vad")
            await asyncio.to_thread(vad_path.write_bytes, data)
        if missing_kws:
            logger.info("voice.model_downloading", model=KWS_MODEL_DIRNAME, dest=str(kws_dir))
            archive = await _download_bytes(KWS_ARCHIVE_URL, client=client, label="kws_archive")
            tar_path = root / f"{KWS_MODEL_DIRNAME}.tar.bz2"
            await asyncio.to_thread(_extract_tar_bz2, tar_path, archive, root)

    keywords_path = await asyncio.to_thread(
        write_keywords_file,
        kws_dir / KEYWORDS_FILENAME,
        wake_words,
        tokens_path=tokens_path,
    )
    cleaned_interrupt = [w for w in interrupt_words if w.strip()]
    interrupt_path: Path | None = None
    if cleaned_interrupt:
        interrupt_path = await asyncio.to_thread(
            write_keywords_file,
            kws_dir / INTERRUPT_KEYWORDS_FILENAME,
            cleaned_interrupt,
            tokens_path=tokens_path,
        )
    return VoiceModelPaths(
        vad_model=vad_path,
        kws_tokens=tokens_path,
        kws_encoder=encoder_path,
        kws_decoder=decoder_path,
        kws_joiner=joiner_path,
        kws_keywords=keywords_path,
        kws_interrupt_keywords=interrupt_path,
    )


async def _download_bytes(
    url: str,
    *,
    client: httpx.AsyncClient | None,
    label: str,
) -> bytes:
    async def _call() -> bytes:
        if client is not None:
            resp = await client.get(url, follow_redirects=True, timeout=120.0)
            resp.raise_for_status()
            return resp.content
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as owned:
            resp = await owned.get(url)
            resp.raise_for_status()
            return resp.content

    try:
        return await retry_async(
            _call,
            attempts=2,
            retry_on=(httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError),
            label=f"voice_download_{label}",
        )
    except Exception as exc:
        raise VoiceError(f"下载语音模型 {label} 失败：{exc}") from exc


def _check_missing_files(vad_path: Path, kws_files: tuple[Path, ...]) -> tuple[bool, bool]:
    return (not vad_path.is_file(), not all(path.is_file() for path in kws_files))


def _extract_tar_bz2(tar_path: Path, payload: bytes, target_dir: Path) -> None:
    tar_path.write_bytes(payload)
    try:
        with tarfile.open(tar_path, "r:bz2") as tf:
            tf.extractall(target_dir, filter="data")
    finally:
        tar_path.unlink(missing_ok=True)


__all__ = [
    "INTERRUPT_KEYWORDS_FILENAME",
    "KEYWORDS_FILENAME",
    "KWS_ARCHIVE_URL",
    "KWS_MODEL_DIRNAME",
    "SILERO_VAD_FILENAME",
    "SILERO_VAD_URL",
    "VoiceModelPaths",
    "encode_wake_word",
    "ensure_voice_models",
    "load_token_set",
    "write_keywords_file",
]
