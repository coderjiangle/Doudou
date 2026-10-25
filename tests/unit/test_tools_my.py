"""my：只读的配置自检工具，以及它的脱敏边界。"""

from __future__ import annotations

from doudou.tools.builtin import self_config
from doudou.tools.decorators import annotations_of
from doudou.tools.registry import build_tools


def _make(settings_factory, yaml: str = "", env: dict | None = None):  # type: ignore[no-untyped-def]
    settings = settings_factory(config_yaml=yaml, env=env or {})
    return self_config.build_tools(settings)[0]


def test_tool_is_read_only(settings_factory) -> None:  # type: ignore[no-untyped-def]
    tool = _make(settings_factory)

    assert tool.name == "my"
    annotations = annotations_of(tool)
    assert annotations.group == "meta"
    assert annotations.side_effects is False


def test_overview_has_key_facts(settings_factory) -> None:  # type: ignore[no-untyped-def]
    tool = _make(
        settings_factory,
        yaml=(
            "runtime:\n  env: local\n  timezone: Asia/Shanghai\n"
            "model:\n  primary: scripted:demo\n  temperature: 0.3\n"
            "agent:\n  name: Tester\n  max_steps: 7\n"
            "server:\n  port: 8123\n"
        ),
    )

    text = tool.invoke({})

    assert "model.primary = scripted:demo" in text
    assert "agent.max_steps = 7" in text
    assert "server.port = 8123" in text
    assert "可用 section" in text


def test_section_and_dotted_key(settings_factory) -> None:  # type: ignore[no-untyped-def]
    tool = _make(
        settings_factory,
        yaml="model:\n  primary: scripted:demo\nagent:\n  max_steps: 7\n",
    )

    assert "model.primary = scripted:demo" in tool.invoke({"section": "model"})
    assert tool.invoke({"section": "agent.max_steps"}) == "agent.max_steps = 7"


def test_unknown_section_lists_available(settings_factory) -> None:  # type: ignore[no-untyped-def]
    tool = _make(settings_factory)

    text = tool.invoke({"section": "nope"})

    assert "没有 section" in text
    assert "model" in text
    assert "agent" in text


def test_credentials_only_report_presence(settings_factory) -> None:  # type: ignore[no-untyped-def]
    secrets = {
        "OPENAI_API_KEY": "sk-super-secret",
        "FEISHU_APP_SECRET": "feishu-secret-value",
        "TAVILY_API_KEY": "tvly-super-secret",
        "DEEPSEEK_API_KEY": "deepseek-super-secret",
        "DOUDOU_API_TOKEN": "api-token-secret",
    }
    tool = _make(settings_factory, env=secrets)

    for section in ("", "all", "credentials", "model", "paths", "channels", "tools"):
        text = tool.invoke({"section": section} if section else {})
        for value in secrets.values():
            assert value not in text

    text = tool.invoke({"section": "credentials"})
    assert "credentials.openai_api_key = true" in text
    assert "credentials.feishu_app_secret = true" in text
    assert "credentials.api_token = true" in text


def test_database_password_is_masked(settings_factory) -> None:  # type: ignore[no-untyped-def]
    tool = _make(
        settings_factory,
        yaml=(
            "storage:\n"
            '  database_url: "postgresql+asyncpg://user:supersecret@localhost:5432/doudou"\n'
        ),
    )

    text = tool.invoke({"section": "storage"})

    assert "supersecret" not in text
    assert "user:********@localhost" in text


def test_tools_section_lists_loaded_tools(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(config_yaml="")
    tools = build_tools(settings)
    my_tool = next(item for item in tools if item.name == "my")

    text = my_tool.invoke({"section": "tools"})

    assert "loaded_names" in text
    assert "now" in text
    assert "my" in text


def test_permissions_are_visible(settings_factory) -> None:  # type: ignore[no-untyped-def]
    tool = _make(
        settings_factory,
        yaml=(
            "agent:\n"
            "  permissions:\n"
            "    - mode: deny\n"
            '      paths: ["/**/.env"]\n'
            '      operations: ["read"]\n'
        ),
    )

    text = tool.invoke({"section": "agent.permissions"})

    assert "agent.permissions[0].mode = deny" in text
    assert "agent.permissions[0].paths[0] = /**/.env" in text


def test_groups_explain_why_a_group_is_off(settings_factory) -> None:  # type: ignore[no-untyped-def]
    settings = settings_factory(config_yaml="", env={})
    my_tool = next(item for item in build_tools(settings) if item.name == "my")

    text = my_tool.invoke({"section": "groups"})

    assert "groups.http.enabled = false" in text
    assert "allowed_domains" in text


def test_section_has_no_write_entrypoint(settings_factory) -> None:  # type: ignore[no-untyped-def]
    """确认工具只暴露一个 section 参数：没有任何修改配置的入口。"""
    tool = _make(settings_factory)

    args = tool.args or {}

    assert set(args) == {"section"}
