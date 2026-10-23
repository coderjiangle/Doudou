"""MCP 清单解析：``${ENV}`` 替换与严格校验。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from doudou.mcp.config import (
    McpConfigError,
    McpServerSpec,
    build_manifest,
    build_spec,
    load_manifest,
    substitute_env,
)


def write_manifest(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "mcp.servers.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


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


def test_unknown_field_rejected(tmp_path: Path) -> None:
    """extra=forbid 挡拼错：``comand`` 这类错误必须立刻可见。"""
    path = write_manifest(
        tmp_path, {"servers": [{"name": "s", "transport": "stdio", "comand": "x"}]}
    )

    with pytest.raises(McpConfigError):
        load_manifest(path, env={})


def test_duplicate_server_names_rejected() -> None:
    with pytest.raises(McpConfigError, match="重复"):
        build_manifest(
            {
                "servers": [
                    {"name": "dup", "transport": "stdio", "command": "x"},
                    {"name": "dup", "transport": "stdio", "command": "y"},
                ]
            }
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

    # 适配器里叫 streamable_http，YAML 里写 http
    assert stdio.mcp_transport_name == "stdio"
    assert http.mcp_transport_name == "streamable_http"
    assert http.source_label == "mcp:h"


# --------------------------------------------------------------------------- 加载文件


def test_load_manifest_full(tmp_path: Path) -> None:
    path = write_manifest(
        tmp_path,
        {
            "version": 1,
            "servers": [
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
            ],
        },
    )

    manifest = load_manifest(path, env={"TENANT": "acme", "TOKEN": "s3cret"})

    assert [server.name for server in manifest.enabled_servers] == ["local", "remote"]
    remote = manifest.by_name("remote")
    assert remote is not None
    assert remote.url == "https://api.test/acme/mcp"
    assert remote.headers["Authorization"] == "Bearer s3cret"
    assert remote.read_only is True


def test_load_manifest_missing_file_is_empty(tmp_path: Path) -> None:
    """MCP 是可选项：没配清单不该阻止服务启动。"""
    manifest = load_manifest(tmp_path / "nope.yaml", env={})

    assert manifest.servers == []


def test_load_manifest_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")

    assert load_manifest(path, env={}).servers == []


def test_load_manifest_bad_yaml(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("servers: [\n", encoding="utf-8")

    with pytest.raises(McpConfigError, match="读取 MCP 清单失败"):
        load_manifest(path, env={})


def test_load_manifest_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")

    with pytest.raises(McpConfigError, match="必须是映射"):
        load_manifest(path, env={})


def test_repo_example_manifest_is_valid() -> None:
    """仓库自带的示例清单必须能通过校验 —— 它是给用户抄的。"""
    example = Path(__file__).resolve().parents[2] / "config" / "mcp.servers.example.yaml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))

    # 示例里有 ${GITHUB_TOKEN} / ${DOUDOU_DATABASE_URL} 占位，用假值替换
    manifest = load_manifest(example, env={"GITHUB_TOKEN": "x", "DOUDOU_DATABASE_URL": "sqlite://"})

    assert manifest.servers, "示例清单不应该为空"
    assert {server.name for server in manifest.servers} >= {"filesystem", "github"}
    del raw
