"""Derived credential view.

凭据现在也是 ``config.yaml`` 的普通字段（只是约定**只放 `.env`**）：

* ``model.api_key`` / ``model.api_keys``
* ``tools.web_search.api_key``
* ``channels.feishu.app_id`` / ``app_secret`` / ``verification_token`` / ``encrypt_key``
* ``observability.tracing.public_key`` / ``secret_key``

环境变量统一为 ``DOUDOU_`` + 配置路径（见 :mod:`doudou.config.env`）。
:class:`Secrets` 只是把这些字段整理成「按 provider 取值」的旧视图，
让模型构造、搜索、追踪等消费方不必各自去翻 ``AppConfig``。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from doudou.config.providers import provider_of
from doudou.config.schema import AppConfig


class Secrets(BaseModel):
    """从 :class:`AppConfig` 派生的凭据视图。"""

    model_config = ConfigDict(frozen=True)

    openai_api_key: str | None = None
    openai_base_url: str | None = None
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    deepseek_api_key: str | None = None
    deepseek_api_base: str | None = None
    openrouter_api_key: str | None = None
    openrouter_base_url: str | None = None
    ollama_host: str | None = None

    azure_openai_api_key: str | None = None
    google_api_key: str | None = None
    google_application_credentials: str | None = None
    groq_api_key: str | None = None
    mistralai_api_key: str | None = None

    tavily_api_key: str | None = None

    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_verification_token: str | None = None
    feishu_encrypt_key: str | None = None
    feishu_base_url: str | None = None

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None

    @classmethod
    def from_config(cls, app: AppConfig) -> Secrets:
        """把配置里的凭据字段按 provider 展开。"""
        primary = provider_of(app.model.primary)
        web = app.tools.web_search
        feishu = app.channels.feishu
        tracing = app.observability.tracing

        def model_key(provider: str) -> str | None:
            key = app.model.api_keys.get(provider)
            if key:
                return key
            return app.model.api_key if provider == primary else None

        def model_base_url(provider: str) -> str | None:
            url = app.model.base_urls.get(provider)
            if url:
                return url
            return app.model.base_url if provider == primary else None

        return cls(
            openai_api_key=model_key("openai"),
            openai_base_url=model_base_url("openai"),
            anthropic_api_key=model_key("anthropic"),
            anthropic_base_url=model_base_url("anthropic"),
            deepseek_api_key=model_key("deepseek")
            or (web.api_key if web.provider == "deepseek" else None),
            deepseek_api_base=model_base_url("deepseek"),
            openrouter_api_key=model_key("openrouter"),
            openrouter_base_url=model_base_url("openrouter"),
            ollama_host=model_base_url("ollama"),
            azure_openai_api_key=model_key("azure_openai"),
            google_api_key=model_key("google_genai"),
            google_application_credentials=model_key("google_vertexai"),
            groq_api_key=model_key("groq"),
            mistralai_api_key=model_key("mistralai"),
            tavily_api_key=web.api_key if web.provider == "tavily" else None,
            feishu_app_id=feishu.app_id,
            feishu_app_secret=feishu.app_secret,
            feishu_verification_token=feishu.verification_token,
            feishu_encrypt_key=feishu.encrypt_key,
            feishu_base_url=feishu.base_url,
            langfuse_public_key=tracing.public_key,
            langfuse_secret_key=tracing.secret_key,
        )

    def as_dict(self) -> dict[str, str | None]:
        """导出为字典，便于日志与 ``config show`` 脱敏展示。"""
        return self.model_dump()


__all__ = ["Secrets"]
