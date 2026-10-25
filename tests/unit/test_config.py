"""配置加载：默认值、YAML、本地覆盖、环境变量优先级与脱敏。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from doudou.config.loader import (
    Settings,
    deep_merge,
    get_settings,
    load_settings,
    reset_settings_cache,
)
from doudou.config.schema import AppConfig
from doudou.config.settings import Secrets
from doudou.security.secrets import mask, redact
from tests.conftest import MINIMAL_CONFIG


def test_defaults_are_usable_without_any_file(tmp_path: Path) -> None:
    """没有 config.yaml 时也能启动：全部走默认值。"""
    settings = load_settings(
        config_dir=tmp_path / "nope", env_file=tmp_path / "no.env", use_env=False
    )

    assert settings.app.agent.name == "Doudou"
    assert settings.app.model.primary == "openai:gpt-5.5"
    assert settings.app.server.port == 8000
    assert settings.config_file is None
    assert settings.loaded_files == ()


def test_yaml_overrides_defaults(settings: Settings) -> None:
    assert settings.app.agent.name == "TestDoudou"
    assert settings.app.agent.max_steps == 4
    assert settings.app.agent.interrupt_on == {"add": True}
    assert settings.config_file is not None
    assert settings.config_file.name == "config.yaml"


def test_local_file_overrides_main(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml=MINIMAL_CONFIG,
        local_yaml="agent:\n  name: LocalName\n  max_steps: 9\n",
    )

    assert settings.app.agent.name == "LocalName"
    assert settings.app.agent.max_steps == 9
    # 未被本地覆盖的字段保持主文件的值
    assert settings.app.runtime.timezone == "Asia/Shanghai"
    assert len(settings.loaded_files) == 2


def test_env_overrides_yaml(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml=MINIMAL_CONFIG,
        env={
            "DOUDOU_AGENT__NAME": "EnvName",
            "DOUDOU_SERVER__PORT": "9123",
            "DOUDOU_MODEL__PRIMARY": "scripted:demo",
        },
    )

    assert settings.app.agent.name == "EnvName"
    assert settings.app.server.port == 9123
    assert settings.app.model.primary == "scripted:demo"


def test_api_token_is_meta_config(settings_factory) -> None:  # type: ignore[no-untyped-def]
    """DOUDOU_API_TOKEN 属于元配置，不应污染 AppConfig（extra=forbid 会报错）。"""
    settings = settings_factory(config_yaml=MINIMAL_CONFIG, env={"DOUDOU_API_TOKEN": "s3cret"})

    assert settings.api_token == "s3cret"


def test_use_env_false_ignores_process_env(settings_factory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DOUDOU_AGENT__NAME", "ShouldBeIgnored")

    settings = settings_factory(config_yaml=MINIMAL_CONFIG, use_env=False)

    assert settings.app.agent.name == "TestDoudou"


def test_unknown_yaml_key_is_rejected(settings_factory) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        settings_factory(config_yaml="agent:\n  max_stpes: 3\n")


def test_secrets_read_from_env(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml=MINIMAL_CONFIG,
        env={"OPENAI_API_KEY": "sk-test", "FEISHU_APP_SECRET": "shh"},
    )

    assert settings.secrets.openai_api_key == "sk-test"
    assert settings.secrets.feishu_app_secret == "shh"


def test_feishu_base_url_secret_is_applied_to_channel(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml=MINIMAL_CONFIG, env={"FEISHU_BASE_URL": "https://open.larksuite.com"}
    )

    assert settings.app.channels.feishu.base_url == "https://open.larksuite.com"


def test_feishu_env_overrides_yaml(settings_factory) -> None:  # type: ignore[no-untyped-def]
    """无前缀的 FEISHU_* 也是环境变量，必须覆盖 config.yaml。"""
    settings = settings_factory(
        config_yaml=MINIMAL_CONFIG
        + (
            "channels:\n"
            "  feishu:\n"
            "    app_id: yaml-app\n"
            "    app_secret: yaml-secret\n"
            "    base_url: https://open.feishu.cn\n"
        ),
        env={
            "FEISHU_APP_ID": "env-app",
            "FEISHU_APP_SECRET": "env-secret",
            "FEISHU_BASE_URL": "https://open.larksuite.com",
        },
    )

    feishu = settings.app.channels.feishu
    assert feishu.app_id == "env-app"
    assert feishu.app_secret == "env-secret"
    assert feishu.base_url == "https://open.larksuite.com"


def test_openai_base_url_env_overrides_yaml(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml=MINIMAL_CONFIG
        + "model:\n  primary: openai:gpt-5.5\n  base_url: https://yaml.example/v1\n",
        env={"OPENAI_BASE_URL": "https://env.example/v1"},
    )

    assert settings.app.model.base_url == "https://env.example/v1"


def test_doudou_override_beats_vendor_env(settings_factory) -> None:  # type: ignore[no-untyped-def]
    """两族环境变量都设置了同一字段时，显式的 DOUDOU_* 优先。"""
    settings = settings_factory(
        config_yaml=MINIMAL_CONFIG + "model:\n  primary: openai:gpt-5.5\n",
        env={
            "OPENAI_BASE_URL": "https://vendor.example/v1",
            "DOUDOU_MODEL__BASE_URL": "https://doudou.example/v1",
        },
    )

    assert settings.app.model.base_url == "https://doudou.example/v1"


def test_doudou_feishu_base_url_beats_vendor_env(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml=MINIMAL_CONFIG,
        env={
            "FEISHU_BASE_URL": "https://lark.example",
            "DOUDOU_CHANNELS__FEISHU__BASE_URL": "https://doudou.example",
        },
    )

    assert settings.app.channels.feishu.base_url == "https://doudou.example"


def test_vendor_base_url_is_provider_scoped(settings_factory) -> None:  # type: ignore[no-untyped-def]
    """OPENAI_BASE_URL 不应该影响非 openai provider。"""
    settings = settings_factory(
        config_yaml=MINIMAL_CONFIG
        + "model:\n  primary: anthropic:claude-sonnet-5\n  base_url: https://yaml.example\n",
        env={"OPENAI_BASE_URL": "https://env.example"},
    )

    assert settings.app.model.base_url == "https://yaml.example"


def test_nested_env_delimiter(settings_factory) -> None:  # type: ignore[no-untyped-def]
    """嵌套字段用 ``__`` 分隔，这是写在 docs/configuration.md 里的约定。"""
    settings = settings_factory(config_yaml=MINIMAL_CONFIG, env={"DOUDOU_SERVER__PORT": "8080"})

    assert settings.app.server.port == 8080


# --------------------------------------------------------------------------- 路径解析


def test_paths_are_resolved_and_normalised(settings: Settings) -> None:
    assert len(settings.skills_dirs) == 1
    # 相对 config_dir 解析，且「..」已被规范化掉
    assert settings.skills_dirs[0] == settings.config_dir.parent / "skills"
    assert all(".." not in str(item) for item in settings.skills_dirs)


def test_system_prompt_and_related_paths(settings: Settings) -> None:
    assert settings.system_prompt_path == settings.config_dir / "prompts" / "system.md"
    assert settings.system_prompt_path is not None
    assert settings.system_prompt_path.is_file()
    assert settings.subagents_path == settings.config_dir / "agents.yaml"
    assert settings.mcp_config_path == settings.config_dir / "mcp.servers.yaml"
    assert settings.root_dir.is_absolute()
    assert ".." not in str(settings.root_dir)


# --------------------------------------------------------------------------- 脱敏


def test_describe_redacts_sensitive_values(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(
        config_yaml=MINIMAL_CONFIG, env={"DOUDOU_API_TOKEN": "topsecret123"}
    )

    described = settings.describe()

    assert described["api_token"] != "topsecret123"
    assert "topsecret123" not in str(described)


def test_redact_only_touches_sensitive_keys() -> None:
    payload = {"api_key": "abcdef123456", "port": 8000, "nested": {"token": "xyzxyzxyz"}}

    redacted = redact(payload)

    assert redacted["port"] == 8000
    assert redacted["api_key"].startswith("abcd")
    assert redacted["api_key"].endswith("********")
    assert redacted["nested"]["token"] != "xyzxyzxyz"


def test_mask_edge_cases() -> None:
    assert mask("") == ""
    assert mask("abc") == "********"


# --------------------------------------------------------------------------- 工具函数


def test_deep_merge() -> None:
    base = {"a": {"b": 1, "c": 2}, "list": [1, 2], "scalar": "x"}
    override = {"a": {"c": 3, "d": 4}, "list": [9], "scalar": "y"}

    merged = deep_merge(base, override)

    assert merged == {"a": {"b": 1, "c": 3, "d": 4}, "list": [9], "scalar": "y"}
    # 不修改入参
    assert base["a"] == {"b": 1, "c": 2}


def test_settings_cache_can_be_reset() -> None:
    reset_settings_cache()
    first = get_settings()
    assert get_settings() is first

    reset_settings_cache()
    assert get_settings() is not first
    reset_settings_cache()


def test_secrets_field_names_match_official_env_names() -> None:
    """无前缀凭据直接沿用官方环境变量名。"""
    assert set(Secrets.model_fields) >= {
        "openai_api_key",
        "anthropic_api_key",
        "feishu_app_secret",
        "tavily_api_key",
    }


def test_app_config_defaults_are_complete() -> None:
    """AppConfig 的所有段落都有默认值，保证无配置文件也能构建。"""
    config = AppConfig()
    assert config.channels.cli.enabled is True
    assert config.mcp.enabled is True
    assert config.storage.checkpointer == "sqlite"
    assert config.agent.skills == ["../skills/"]
