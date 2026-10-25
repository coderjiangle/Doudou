"""Provider 元数据：凭据与 base_url 的官方命名。

配置层和模型工厂共用这一份，避免两边各写一套：

* ``PROVIDER_CREDENTIALS`` 用来判断某个 provider 是否需要 API Key，以及
  旧版环境变量名（仅作兼容提示）；
* ``PROVIDER_BASE_URL_ENV`` 把 provider 映射到 ``Secrets`` 里的 base_url 字段，
  新配置优先用 ``model.base_urls`` / ``model.base_url``。

新代码请不要直接读这些厂商环境变量 —— 统一走 ``DOUDOU_*`` 配置路径
（见 :mod:`doudou.config.env`）。
"""

from __future__ import annotations

#: provider -> 该 provider 需要的凭据。值为旧版官方环境变量名，仅用于提示与兼容。
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

#: provider -> ``Secrets`` 里对应的 base_url 字段名。
#: 变量名小写后与 :class:`~doudou.config.settings.Secrets` 的字段一一对应。
PROVIDER_BASE_URL_ENV: dict[str, str] = {
    "openai": "OPENAI_BASE_URL",
    "deepseek": "DEEPSEEK_API_BASE",
    "anthropic": "ANTHROPIC_BASE_URL",
    "openrouter": "OPENROUTER_BASE_URL",
    "ollama": "OLLAMA_HOST",
}


def provider_of(spec: str) -> str | None:
    """从 ``provider:model`` 里取出 provider；没有冒号时返回 ``None``。"""
    provider, sep, _ = spec.strip().partition(":")
    return provider.strip().lower() if sep else None


__all__ = ["PROVIDER_BASE_URL_ENV", "PROVIDER_CREDENTIALS", "provider_of"]
