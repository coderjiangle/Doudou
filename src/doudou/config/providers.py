"""Provider 元数据：凭据与 base_url 的官方环境变量名。

放在 ``config`` 层，是因为配置加载需要用它把「无前缀的厂商变量」映射到
``model.base_url`` 与 ``Secrets``；``agent.models`` 也复用同一份，避免两边各写一套。

约定：

* 凭据沿用各家官方命名，只有 ``Secrets`` 声明的才会读 ``.env``；
* ``base_url`` 同样是厂商变量，优先级低于 ``DOUDOU_MODEL__BASE_URL``、
  高于 ``config.yaml`` 的 ``model.base_url``。
"""

from __future__ import annotations

#: provider -> 该 provider 需要的环境变量（用于报错提示与凭据检查）。
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
