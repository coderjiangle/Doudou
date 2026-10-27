"""MCP 配置解析：``${ENV}`` 替换、defaults 继承与严格校验。

清单写在 ``config.yaml`` 的 ``mcp.servers`` 里，结构由 :mod:`doudou.config.schema`
定义；这里测的是「结构 → 运行期清单」这一段（占位替换 + 组装）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from doudou.config.loader import load_yaml
from doudou.config.schema import McpConfig, McpServerSpec
from doudou.mcp.config import (
    McpConfigError,
    build_spec,
    manifest_from_config,
    substitute_env,
)


def make_config(**payload: object) -> McpConfig:
    """构造一个 ``mcp`` 段（等价于 config.yaml 里写的那块）。"""
    return McpConfig.model_validate(payload)


# --------------------------------------------------------------------------- 环境变量替换


def test_substitute_env_in_nested_values() -> None:
    value = {
        "url": "https://api.test/${TENANT}/mcp",
        "headers": {"Authorization": "Bearer ${TOKEN}"},
        "args": ["--token", "${TOKEN}"],
        "count": 3,
    }

    result = substitute_env(value, env={"TENANT": "acme", "TOKEN": "s3cret"})

    assert result == {
        "url": "https://api.test/acme/mcp",
        "headers": {"Authorization": "Bearer s3cret"},
        "args": ["--token", "s3cret"],
        "count": 3,
    }


def test_substitute_env_missing_variable_raises() -> None:
    """必须报错而不是留原样：把 ``Bearer ${TOKEN}`` 发出去只会得到莫名其妙的 401。"""
    with pytest.raises(McpConfigError, match=r"\$\{TOKEN\}"):
        substitute_env({"h": "${TOKEN}"}, env={})


def test_substitute_env_leaves_non_strings() -> None:
    assert substitute_env([1, None, True], env={}) == [1, None, True]


# --------------------------------------------------------------------------- 校验


def test_stdio_requires_command() -> None:
    with pytest.raises(McpConfigError, match="必须提供 command"):
        build_spec({"name": "bad", "transport": "stdio"})


def test_stdio_rejects_url() -> None:
    with pytest.raises(McpConfigError, match="不应该有 url"):
        build_spec({"name": "bad", "transport": "stdio", "command": "x", "url": "https://x"})


def test_http_requires_url() -> None:
    with pytest.raises(McpConfigError, match="必须提供 url"):
        build_spec({"name": "bad", "transport": "http"})


def test_http_rejects_command() -> None:
    with pytest.raises(McpConfigError, match="不应该有 command"):
        build_spec({"name": "bad", "transport": "http", "url": "https://x", "command": "y"})


@pytest.mark.parametrize("name", ["有中文", "with space", "-leading", "a/b", ""])
def test_invalid_server_names(name: str) -> None:
    """server 名会成为工具前缀，因此限制字符集。"""
    with pytest.raises(McpConfigError, match="不合法"):
        build_spec({"name": name, "transport": "stdio", "command": "x"})


def test_include_exclude_overlap_rejected() -> None:
    with pytest.raises(McpConfigError, match="同时包含"):
        build_spec(
            {
                "name": "ok",
                "transport": "stdio",
                "command": "x",
                "include_tools": ["a"],
                "exclude_tools": ["a"],
            }
        )


def test_build_spec_returns_spec() -> None:
    spec = build_spec({"name": "svc", "transport": "stdio", "command": "python"})

    assert isinstance(spec, McpServerSpec)
    assert spec.name == "svc"


def test_unknown_field_rejected() -> None:
    """extra=forbid 挡拼错：``comand`` 这类错误必须在**配置校验**阶段就可见。"""
    with pytest.raises(ValidationError) as excinfo:
        make_config(servers=[{"name": "s", "transport": "stdio", "comand": "x"}])

    assert "comand" in str(excinfo.value)


def test_duplicate_server_names_rejected() -> None:
    with pytest.raises(ValidationError, match="重复"):
        make_config(
            servers=[
                {"name": "dup", "transport": "stdio", "command": "x"},
                {"name": "dup", "transport": "stdio", "command": "y"},
            ]
        )


# --------------------------------------------------------------------------- 工具面


def test_tool_allowed_without_filters() -> None:
    spec = McpServerSpec(name="s", transport="stdio", command="x")

    assert spec.tool_allowed("anything") is True


def test_include_list_wins() -> None:
    spec = McpServerSpec(name="s", transport="stdio", command="x", include_tools=["a", "b"])

    assert spec.tool_allowed("a") is True
    assert spec.tool_allowed("c") is False


def test_exclude_list_blocks_only_listed() -> None:
    spec = McpServerSpec(name="s", transport="stdio", command="x", exclude_tools=["delete"])

    assert spec.tool_allowed("read") is True
    assert spec.tool_allowed("delete") is False


def test_read_only_defaults_to_false() -> None:
    """默认按「有副作用」处理 —— 猜错的方向必须是多问一次，而不是默默删了。"""
    assert McpServerSpec(name="s", transport="stdio", command="x").read_only is False


def test_transport_name_mapping() -> None:
    stdio = McpServerSpec(name="s", transport="stdio", command="x")
    http = McpServerSpec(name="h", transport="http", url="https://x")

    # 适配器里叫 streamable_http，配置里写 http
    assert stdio.mcp_transport_name == "stdio"
    assert http.mcp_transport_name == "streamable_http"
    assert http.source_label == "mcp:h"


# --------------------------------------------------------------------------- defaults 继承


def test_defaults_are_inherited_unless_set() -> None:
    """``mcp.defaults`` 只填没被显式写过的字段。"""
    config = make_config(
        defaults={"timeout": 5, "read_only": True, "retries": 3},
        servers=[
            {"name": "inherits", "transport": "stdio", "command": "x"},
            {
                "name": "explicit",
                "transport": "stdio",
                "command": "y",
                "timeout": 60,
                "read_only": False,
            },
        ],
    )

    inherits, explicit = config.servers
    assert (inherits.timeout, inherits.read_only, inherits.retries) == (5, True, 3)
    assert (explicit.timeout, explicit.read_only) == (60, False)
    assert explicit.retries == 3  # 没写的字段仍然继承


# --------------------------------------------------------------------------- 组装清单


def test_manifest_from_config_substitutes_env() -> None:
    config = make_config(
        servers=[
            {"name": "local", "transport": "stdio", "command": "python", "args": ["a"]},
            {
                "name": "remote",
                "transport": "http",
                "url": "https://api.test/${TENANT}/mcp",
                "headers": {"Authorization": "Bearer ${TOKEN}"},
                "read_only": True,
                "include_tools": ["search"],
            },
            {"name": "off", "transport": "stdio", "command": "x", "enabled": False},
        ]
    )

    manifest = manifest_from_config(config, env={"TENANT": "acme", "TOKEN": "s3cret"})

    assert [server.name for server in manifest.enabled_servers] == ["local", "remote"]
    remote = manifest.by_name("remote")
    assert remote is not None
    assert remote.url == "https://api.test/acme/mcp"
    assert remote.headers["Authorization"] == "Bearer s3cret"
    assert remote.read_only is True


def test_manifest_from_config_without_servers_is_empty() -> None:
    """MCP 是可选项：没配 server 不该影响启动。"""
    manifest = manifest_from_config(McpConfig())

    assert manifest.servers == []
    assert manifest.enabled_servers == []


def test_manifest_from_config_missing_env_raises() -> None:
    """占位符变量缺失必须报错，而不是把 ``Bearer ${TOKEN}`` 原样发出去换 401。"""
    config = make_config(
        servers=[
            {
                "name": "gh",
                "transport": "http",
                "url": "https://api.test/mcp",
                "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
            }
        ]
    )

    with pytest.raises(McpConfigError, match=r"\$\{GITHUB_TOKEN\}"):
        manifest_from_config(config, env={})


def test_repo_config_mcp_section_is_valid() -> None:
    """仓库 config.yaml 的 mcp 段必须能通过校验 —— 它是给用户抄的样板。"""
    repo = Path(__file__).resolve().parents[2]
    raw = load_yaml(repo / "config" / "config.yaml")

    mcp = McpConfig.model_validate(raw["mcp"])

    assert mcp.enabled is True
    assert mcp.servers == []
    # 独立清单文件已经合并进 config.yaml
    assert not (repo / "config" / "mcp.servers.example.yaml").exists()
    assert not (repo / "config" / "mcp.servers.yaml").exists()
