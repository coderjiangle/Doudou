"""Chat model factory: provider, base_url, fallbacks, retries."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from doudou.agent.testing import demo_model
from doudou.config.schema import ModelConfig
from doudou.config.settings import Secrets
from doudou.observability.logging import get_logger

logger = get_logger(__name__)

#: 离线模型前缀：``scripted:demo``。
SCRIPTED_PREFIX = "scripted:"

#: provider -> 该 provider 需要的环境变量（仅用于给出可操作的报错提示）。
PROVIDER_CREDENTIALS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "google_vertexai": "GOOGLE_APPLICATION_CREDENTIALS",
    "groq": "GROQ_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama": "",
}

#: provider -> 该 provider 读取 base_url 的官方环境变量名。
PROVIDER_BASE_URL_ENV: dict[str, str] = {
    "openai": "OPENAI_BASE_URL",
    "deepseek": "DEEPSEEK_API_BASE",
    "anthropic": "ANTHROPIC_BASE_URL",
    "openrouter": "OPENROUTER_BASE_URL",
    "ollama": "OLLAMA_HOST",
}


class ModelBuildError(RuntimeError):
    """无法构造模型时抛出，消息里会带上可操作的修复建议。"""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """解析后的 ``provider:model`` 描述。"""

    raw: str
    provider: str | None
    model: str

    @property
    def is_scripted(self) -> bool:
        return self.provider == "scripted"


def parse_spec(spec: str) -> ModelSpec:
    """拆分 ``provider:model``。

    - ``openai:gpt-5.5``   → provider=openai, model=gpt-5.5
    - ``gpt-5.5``          → provider=None（由 langchain 自行推断）
    - ``scripted:demo``    → 内置离线模型
    """
    raw = spec.strip()
    if not raw:
        raise ModelBuildError("模型标识为空，请在 config 的 model.primary 或 --model 中指定")
    provider, sep, model = raw.partition(":")
    if not sep:
        return ModelSpec(raw=raw, provider=None, model=raw)
    # ollama:llama3 这类没有冒号的 provider 前缀也要能识别
    return ModelSpec(raw=raw, provider=provider.strip().lower(), model=model.strip())


def credential_hint(spec: ModelSpec) -> tuple[str, str] | None:
    """返回 ``(环境变量名, 提示语)``；该 provider 不需要凭据时返回 ``None``。"""
    if spec.provider is None or spec.is_scripted:
        return None
    env_name = PROVIDER_CREDENTIALS.get(spec.provider)
    if env_name is None:
        # 未知 provider：langchain 自己会报错，这里不拦。
        return None
    if not env_name:
        return None
    return env_name, f"模型 {spec.raw} 需要 {env_name}，请在 .env 中设置"


def build_model(
    config: ModelConfig,
    *,
    secrets: Secrets | None = None,
    spec: str | None = None,
    extra: dict[str, Any] | None = None,
) -> BaseChatModel:
    """按配置构造聊天模型。

    Args:
        config: ``config.model`` 段。
        secrets: 凭据来源；缺省时由 langchain 直接读环境变量。
        spec: 覆盖 ``config.primary``（CLI 的 ``--model``）。
        extra: 追加给 ``init_chat_model`` 的参数。

    Raises:
        ModelBuildError: 标识非法、缺少 provider 包或缺少凭据。
    """
    resolved = parse_spec(spec or config.primary)

    if resolved.is_scripted:
        return _build_scripted(resolved)

    kwargs: dict[str, Any] = {
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "timeout": config.timeout,
        "max_retries": config.max_retries,
    }
    base_url = config.base_url or _secret_base_url(resolved, secrets)
    if base_url:
        kwargs["base_url"] = base_url
    api_key = _secret_api_key(resolved, secrets)
    if api_key:
        kwargs["api_key"] = api_key
    kwargs.update(extra or {})
    kwargs = {key: value for key, value in kwargs.items() if value is not None}

    from langchain.chat_models import init_chat_model

    try:
        return cast("BaseChatModel", init_chat_model(resolved.raw, **kwargs))
    except ImportError as exc:
        provider = resolved.provider or "<无法推断>"
        raise ModelBuildError(
            f"缺少 provider {provider!r} 的依赖包。安装对应的 langchain 集成包后重试，"
            f"例如 `uv sync --extra anthropic`。原始错误：{exc}"
        ) from exc
    except Exception as exc:
        hint = credential_hint(resolved)
        suffix = f"\n提示：{hint[1]}" if hint else ""
        raise ModelBuildError(f"构造模型 {resolved.raw!r} 失败：{exc}{suffix}") from exc


def _build_scripted(resolved: ModelSpec) -> BaseChatModel:
    """内置离线模型：不访问网络，用于 CI 与本地自检。"""
    name = resolved.model or "demo"
    if name in {"demo", "offline"}:
        logger.info("model.scripted", name=name)
        return demo_model()
    if name.startswith("echo:"):
        return GenericFakeChatModel(messages=iter([name.removeprefix("echo:")]))
    raise ModelBuildError(f"未知的离线模型 {name!r}，可用值：scripted:demo、scripted:echo:<文本>")


def _secret_api_key(resolved: ModelSpec, secrets: Secrets | None) -> str | None:
    """从凭据对象取该 provider 的 API Key。"""
    if secrets is None or resolved.provider is None:
        return None
    field = PROVIDER_CREDENTIALS.get(resolved.provider)
    if not field:
        return None
    return getattr(secrets, field.lower(), None)


def _secret_base_url(resolved: ModelSpec, secrets: Secrets | None) -> str | None:
    """从凭据对象取该 provider 的 base_url（目前仅 OpenAI 兼容网关需要）。"""
    if secrets is None or resolved.provider is None:
        return None
    if resolved.provider == "openai":
        return secrets.openai_base_url
    return None


def has_credential(secrets: Secrets, spec: ModelSpec) -> bool:
    """判断该模型所需的凭据是否已就绪（配置对象或进程环境变量）。"""
    env_name = PROVIDER_CREDENTIALS.get(spec.provider or "")
    if not env_name:
        return True
    if _secret_api_key(spec, secrets):
        return True
    return bool(os.environ.get(env_name))


def build_fallback_models(
    config: ModelConfig, *, secrets: Secrets | None = None
) -> list[BaseChatModel]:
    """构造 fallback 模型列表；单个失败只告警，不影响主链路。"""
    models: list[BaseChatModel] = []
    for spec in config.fallbacks:
        try:
            models.append(build_model(config, secrets=secrets, spec=spec))
        except ModelBuildError as exc:
            logger.warning("model.fallback_build_failed", spec=spec, error=str(exc))
    return models


__all__ = [
    "PROVIDER_CREDENTIALS",
    "SCRIPTED_PREFIX",
    "ModelBuildError",
    "ModelSpec",
    "build_fallback_models",
    "build_model",
    "credential_hint",
    "has_credential",
    "parse_spec",
]
